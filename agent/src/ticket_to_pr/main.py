"""Invocations protocol server, version 1.0.0 (docs/architecture.md §2, docs/contract.md).

``POST /invocations`` validates the request, registers a run and returns 202 immediately;
the coding run continues in the background and the caller polls
``GET /invocations/{runId}``.

Inbound authorization is the hosting platform's job: Foundry validates the caller's Entra
token for the Agent Application before the request reaches this process. This service
therefore never reads the ``Authorization`` header, and never logs it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import ado
from .config import ConfigError, HarnessName, Settings
from .harness import Harness, get_harness
from .models import InvocationAccepted, InvocationRequest, RunState, RunStatus
from .redact import register_secret
from .run import Orchestrator, RunStore
from .runner import RUNNER_FD_ENV

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0.0"


class AppState:
    """Everything built once at startup and shared by every request."""

    def __init__(self, cfg: Settings, harness: Harness, tokens: ado.TokenProvider) -> None:
        self.cfg = cfg
        self.harness = harness
        self.tokens = tokens
        self.store = RunStore()
        self.orchestrator = Orchestrator(cfg, harness, tokens)
        #: Strong references to in-flight tasks; without these asyncio may collect them.
        self.tasks: set[asyncio.Task[None]] = set()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )


def log_cli_versions() -> None:
    """docs/architecture.md §4.4: print both CLI versions into the log at startup."""
    for executable in ("git", "node", "codex", "claude"):
        path = shutil.which(executable)
        if path is None:
            log.info("cli %s: not installed", executable)
            continue
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, resolved via shutil.which
                [path, "--version"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("cli %s: version check failed (%s)", executable, type(exc).__name__)
            continue
        log.info("cli %s: %s", executable, (proc.stdout or proc.stderr).strip().splitlines()[:1])


#: Headers the Foundry gateway may carry its own invocation id in. The gateway validates
#: the id format on ``GET /invocations/{id}`` and rejects anything that is not one of its
#: own with ``400 Invalid invocation ID format``, so a run keyed by a locally-generated
#: UUID cannot be polled through the gateway at all.
PLATFORM_INVOCATION_HEADERS = (
    "x-agent-invocation-id",
    "x-ms-agent-invocation-id",
    "agent-invocation-id",
)

#: What an invocation id may look like before this process will adopt it as a run id. A
#: header is caller-controlled input until something checks it, and this one reaches a
#: ``Location`` response header, INFO log lines and a filesystem path prefix. Unbounded, a
#: raw value carries CRLF (header and log injection), path separators, and megabytes.
#:
#: Deliberately a superset of both id shapes actually in play -- the gateway's GUID and a
#: local ``uuid4`` -- and no more: alphanumerics, dot, underscore, hyphen, 1-128 characters.
RUN_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


def _platform_invocation_id(request: Request) -> str | None:
    """The gateway's invocation id, if it told us a usable one.

    ``None`` -- which makes :meth:`RunStore.create` mint a UUID -- rather than a 400 for a
    malformed value. The *platform* sets this header, not the caller of the contract, so a
    bad one is the hosting environment misbehaving, not a client error to report as one;
    refusing the invocation would take a run down for something the caller cannot fix. The
    cost is that polling through the gateway will not correlate, which the warning says.
    """
    for name in PLATFORM_INVOCATION_HEADERS:
        value = request.headers.get(name)
        if not value:
            continue
        if RUN_ID_RE.match(value):
            return value
        # Length and character class only. The value itself never reaches the log: it is
        # the thing suspected of carrying CRLF.
        log.warning(
            "ignoring the %s header: %d characters, not the expected id format; "
            "this run will be keyed by a generated id and cannot be polled through "
            "the gateway",
            name,
            len(value),
        )
    return None


def create_app(cfg: Settings | None = None) -> FastAPI:
    settings = cfg or Settings.from_env()
    configure_logging(settings.log_level)

    # Before anything can log or build a pull request body. The PAT is the one secret this
    # process holds by value, and shape-based redaction alone would not catch it -- a PAT
    # is an undistinguished alphanumeric string (docs/deploy.md §3).
    register_secret(settings.ado_pat)

    # The harness runs as its own uid and shares the run directory with this process
    # through one group (see ticket_to_pr.runner). Sharing the *directory* is not enough:
    # git clone writes the working tree with the default 022 umask, so every file and
    # subdirectory it creates is group read-only and the harness cannot edit -- or even
    # create files inside -- the repository it was handed. The symptom is not a permission
    # error but `harness_no_changes`, because the CLI reports success having written
    # nothing. 002 makes what this process writes group-writable; the runner already sets
    # the same umask on its side.
    if RUNNER_FD_ENV in os.environ:
        os.umask(0o002)

    harness = get_harness(settings.harness)
    harness.preflight(settings)
    tokens = ado.build_token_provider(settings)
    state = AppState(settings, harness, tokens)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_cli_versions()
        log.info(
            "ticket-to-pr ready: protocol %s, harness %s, model %s, orgs %s, dev_mode=%s",
            PROTOCOL_VERSION,
            settings.harness.value,
            settings.model_deployment,
            sorted(settings.allowed_orgs),
            settings.dev_mode,
        )
        if settings.dev_mode:
            log.warning(
                "DEV MODE IS ON: Azure DevOps calls go to %s with a static token and "
                "the listener is bound to loopback only. Never use this configuration "
                "anywhere but a developer machine.",
                settings.ado_base_url,
            )
        try:
            yield
        finally:
            for task in list(state.tasks):
                task.cancel()
            # A cancelled run may have started deleting an orphaned branch. Wait for it:
            # The no-dangling-branch rule (docs/architecture.md §3) does not have a
            # "unless we were shutting down" clause.
            await state.orchestrator.drain()
            await tokens.aclose()

    app = FastAPI(
        title="ticket-to-pr-agent",
        version=PROTOCOL_VERSION,
        summary="Turns an Azure DevOps work item into a branch and a pull request.",
        lifespan=lifespan,
    )
    app.state.agent = state

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """docs/contract.md §3: unknown or malformed fields are a 400, not a 422."""
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "bad_request", "details": _safe_errors(exc)}},
        )

    @app.get("/healthz")
    @app.get("/readiness")
    async def healthz() -> dict[str, str]:
        """200 once configuration validation has passed.

        ``/healthz`` is the documented liveness route (docs/contract.md §1). ``/readiness``
        is the
        Foundry hosted-agent runtime contract's health probe: "Return ``200 OK`` from
        ``GET /readiness``" -- the platform uses it to decide the agent version is ready,
        so without it the version never reaches ``active``. The SDK adapter packages
        register that route for you; this container implements the Invocations protocol
        directly, so it has to serve the probe itself. The contract pins the status code
        only and says nothing about a body, so both paths return the same payload.
        """
        # Reaching this point at all means Settings.from_env and preflight both passed.
        return {"status": "ok", "protocol": PROTOCOL_VERSION, "harness": state.harness.name}

    @app.post(
        "/invocations",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=InvocationAccepted,
    )
    async def invoke(
        request: InvocationRequest, response: Response, http_request: Request
    ) -> InvocationAccepted:
        # The Foundry gateway mints its own invocation id and validates the id format on
        # GET, rejecting anything that is not its own with 400. Adopting it as the run id
        # is what makes polling work through the gateway (docs/contract.md §4). Header
        # names only -- never values, which is where an Authorization header would be.
        log.debug("platform headers on POST: %s", sorted(http_request.headers.keys()))
        platform_id = _platform_invocation_id(http_request)
        run = state.store.create(request.correlation_id, run_id=platform_id)
        # Log identifiers only. Work item bodies and titles never reach the log at INFO.
        log.info(
            "run %s queued: org=%s project=%s repo=%s work_item=%s branch=%s",
            run.run_id,
            request.organization,
            request.project,
            request.repository,
            request.work_item.id,
            request.branch,
        )
        task = asyncio.create_task(state.orchestrator.execute(run, request))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        response.headers["Location"] = f"/invocations/{run.run_id}"
        return InvocationAccepted(
            run_id=run.run_id, status=RunState.QUEUED, correlation_id=run.correlation_id
        )

    @app.get("/invocations/{run_id}", response_model=RunStatus)
    async def get_run(run_id: str) -> Any:
        run = state.store.get(run_id)
        if run is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": {"code": "not_found", "message": "unknown run id"}},
            )
        return run.status()

    return app


def _safe_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Field-level validation detail without echoing the submitted values back."""
    out: list[dict[str, str]] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
        out.append({"field": location or "body", "reason": str(err.get("msg", "invalid"))})
    return out


def main() -> int:
    """Console entry point.

    Runs behind :mod:`ticket_to_pr.launch`, which scrubs the managed-identity plumbing
    out of the exec-time environment and hands it back on an inherited fd. Draining that
    fd is the first thing that happens, before any socket is bound or any harness spawned,
    so ``DefaultAzureCredential`` finds the values in ``os.environ`` while
    ``/proc/<pid>/environ`` never does. A no-op when launched directly (tests, dev mode).
    """
    import uvicorn

    from .launch import absorb_credentials

    absorb_credentials()

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if settings.harness is HarnessName.FAKE:
        configure_logging(settings.log_level)
        log.warning("running with the fake harness; no model will be called")

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
