"""Harness abstraction (docs/architecture.md §4.2): the protocol, the result type and the
factory.

A harness is the thing that actually edits the working tree. It receives the repository
files and model access and nothing else -- in particular never an Azure DevOps token
(docs/architecture.md §4.2, §6). :func:`clean_env` is the single place that decides what a harness
subprocess is allowed to see, and it is an allowlist, not a filter.

"Model access" is deliberately a *pre-scoped token*, never the means to obtain one. An
earlier version of this module allow-listed the managed-identity plumbing
(``IDENTITY_ENDPOINT``/``IDENTITY_HEADER``, ``MSI_ENDPOINT``/``MSI_SECRET``,
``AZURE_FEDERATED_TOKEN_FILE`` with ``AZURE_CLIENT_ID`` and ``AZURE_TENANT_ID``) so that
each CLI could mint its own credential. That defeated the rule completely: the identity
endpoint issues a token for *whatever audience the caller asks for*, so a work-item
description -- attacker-controlled text that reaches the harness verbatim in the prompt --
could tell the harness to request ``resource=499b84ac-1321-427f-aa17-267ca6975798`` and
walk off with an Azure DevOps bearer token holding write access to every repository the
agent identity can reach.

So the parent process keeps the credential. :func:`foundry_token` mints one token for
:data:`FOUNDRY_SCOPE` -- inference, and nothing else -- per run, and the harness modules
hand the *value* to their CLI. A harness that dumps its whole environment leaks an
inference token and nothing more.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import ConfigError, HarnessName, Settings
from ..models import ErrorCode, RunFailure
from ..runner import RUNNER_FD_ENV, recv_frame, send_frame

log = logging.getLogger(__name__)

#: Entra scope for data-plane inference against a Foundry model deployment. The only
#: audience the agent ever mints a token for on a harness's behalf, and pointedly not the
#: Azure DevOps audience ``499b84ac-1321-427f-aa17-267ca6975798``.
#:
#: ``ai.azure.com``, not ``cognitiveservices.azure.com``. Both look plausible and only one
#: works: a token for the Cognitive Services audience is rejected by the Foundry model
#: endpoint with ``401 ... audience is incorrect (https://ai.azure.com)``, which is the
#: error naming the audience it actually wanted. Verified against a live deployment.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

#: Everything a harness subprocess is permitted to inherit. Anything not named here --
#: every ``T2PR_*`` setting, every ``GIT_CONFIG_*`` value, the Azure DevOps token, and
#: every managed-identity variable that would let the harness mint a token of its own --
#: is dropped. Nothing in this tuple is exchangeable for a credential: they are search
#: paths, locales, CA bundles and one non-secret platform endpoint.
#:
#: ``HOME`` is pointedly *not* here. Inheriting the server's home directory let a harness
#: plant ``$HOME/.local/lib/python*/site-packages/usercustomize.py`` or ``$HOME/.profile``,
#: which any later non-isolated interpreter or login shell started by that uid would
#: execute -- with the container's unscrubbed configuration environment, including the
#: identity plumbing. :func:`clean_env` gives each run its own throwaway ``HOME`` instead.
ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    # Injected by the Foundry platform (docs/architecture.md §2). A URL, not a credential:
    # calling it still needs a bearer token, and none is reachable from this environment.
    "FOUNDRY_PROJECT_ENDPOINT",
)

#: Values :func:`clean_env` *sets* rather than inherits. Kept separate from
#: :data:`ENV_ALLOWLIST` so the allowlist keeps meaning exactly "what may cross over from
#: the server's environment", and the test that pins that property stays honest.
ENV_SET_BY_AGENT = (
    "HOME",
    "PYTHONNOUSERSITE",
    "DO_NOT_TRACK",
)

#: Name fragments that must never appear in :data:`ENV_ALLOWLIST`. Each is a step on some
#: path to a token the harness must not be able to obtain, or a token itself; the test
#: suite asserts the allowlist contains none of them, so re-adding the identity plumbing
#: fails the build.
#:
#: :data:`ticket_to_pr.launch.CREDENTIAL_NAME_FRAGMENTS` is the same tuple, used for the
#: other half of the same job -- keeping these names off the *exec-time* environment, where
#: ``/proc/<pid>/environ`` would hand them to a same-uid reader. ``test_launch.py`` pins the
#: two against each other, because a fragment added to one and not the other is exactly the
#: gap the Azure DevOps PAT fell through: ``T2PR_ADO_PAT`` matched nothing here and so was
#: scrubbed from neither list.
#:
#: ``ADO_PAT``, not ``PAT``: matching is substring on the upper-cased name, and ``PATH``
#: contains ``PAT``. Stripping ``PATH`` would break every subprocess spawn in the image.
FORBIDDEN_ENV_FRAGMENTS = (
    "IDENTITY",
    "MSI",
    "IMDS",
    "FEDERATED",
    "AZURE_CLIENT",
    "AZURE_TENANT",
    "AZURE_AUTHORITY",
    "DEVOPS",
    "ADO_PAT",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)


@dataclass(frozen=True, slots=True)
class HarnessResult:
    exit_code: int
    summary: str
    stdout: str
    stderr: str


@runtime_checkable
class Harness(Protocol):
    """docs/architecture.md §4.2."""

    name: str

    def preflight(self, cfg: Settings) -> None:
        """Verify the CLI is present and the model endpoint is configured. Raises on failure."""
        ...

    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult: ...


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the harness environment from an allowlist plus explicit additions."""
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if os.environ.get(name)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    # Never the server's $HOME (see ENV_ALLOWLIST). Callers pass the per-run directory;
    # the fallback exists only so clean_env() is callable without one in tests.
    env.setdefault("HOME", tempfile.gettempdir())
    # Ignore ~/.local site-packages, so a usercustomize.py left behind by an earlier run
    # cannot execute inside a later one.
    env["PYTHONNOUSERSITE"] = "1"
    # Neither CLI should phone home about usage from inside a customer's tenant.
    env["DO_NOT_TRACK"] = "1"
    if extra:
        env.update(extra)
    return env


@contextlib.contextmanager
def harness_scratch_dir(repo_dir: Path, prefix: str) -> Iterator[Path]:
    """A temporary directory the *harness uid* can write, deleted with the block.

    ``tempfile.mkdtemp`` creates 0700 owned by the caller, and the caller here is the
    server at uid 10001 while the CLI runs at 10002. A CLI told to write into such a
    directory fails silently: ``codex exec --output-last-message`` cannot create the file,
    reports no error, and :func:`ticket_to_pr.harness.codex._read_summary` quietly falls
    back to stdout -- a degraded summary with nothing in the log to explain it.

    So the scratch lives *inside the run directory*, which :func:`ticket_to_pr.run._make_run_dir`
    has already made 2770 in the shared group. The setgid bit gives this directory that
    group, and the 002 umask makes it group-writable; the harness writes as 10002, and the
    server reads the result back as 10001 through the same group.

    Without a runner (tests, dev mode, non-root container) everything is one uid anyway and
    the chmod is a harmless no-op on a directory the caller already owns.
    """
    with tempfile.TemporaryDirectory(prefix=prefix, dir=repo_dir.parent) as raw:
        scratch = Path(raw)
        try:
            scratch.chmod(0o2770)
        except OSError as exc:  # pragma: no cover - only a nonstandard filesystem
            log.warning("could not share %s with the harness group: %s", prefix, exc)
        yield scratch


#: How long a killed process group gets to die politely before SIGKILL.
KILL_GRACE_SECONDS = 5.0

_credential: Any | None = None


def foundry_token(*, run_seconds: int, scope: str = FOUNDRY_SCOPE) -> str:
    """Mint one inference-audience bearer token for a harness subprocess.

    The parent holds the managed identity; the harness holds only this token's value, so
    the worst a prompt-injected harness can do with its environment is call the model
    deployment it was already given.

    Token lifetime: Entra access tokens for :data:`FOUNDRY_SCOPE` are typically valid for
    an hour or more, comfortably longer than the 20-minute ``T2PR_MAX_RUN_MINUTES``
    default. Neither CLI can refresh a token supplied this way, so if the remaining
    lifetime is shorter than the run budget -- possible once ``T2PR_MAX_RUN_MINUTES`` is
    raised towards its 240-minute ceiling -- the CLI will start getting 401s partway
    through and the run ends as ``harness_failed``. That is logged loudly here rather than
    guessed at, because the alternative (refusing to start a run that would probably have
    finished in time) is worse.
    """
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    try:
        access = _credential.get_token(scope)
    except Exception as exc:
        raise RunFailure(
            ErrorCode.HARNESS_FAILED,
            "could not obtain a Microsoft Foundry inference token for the agent identity",
        ) from exc
    remaining = float(access.expires_on) - time.time()
    if remaining < run_seconds:
        log.warning(
            "the Foundry inference token has %ds left but the run budget is %ds; the "
            "harness cannot refresh it and may fail with 401 near the end of the run. "
            "Lower T2PR_MAX_RUN_MINUTES.",
            int(remaining),
            run_seconds,
        )
    return str(access.token)


def _terminate_group(proc: subprocess.Popen[str], pgid: int | None = None) -> None:
    """Kill the harness *and everything it started*, on **every** exit path.

    ``subprocess`` only ever signals the direct child, and both CLIs spawn their own
    children (test runners, language servers, build tools). Without this a run leaves
    grandchildren in the container, still writing to the working tree that
    :mod:`ticket_to_pr.run` is about to delete -- and, since the container outlives the run,
    into the *next* run's working tree, after its diff has already been counted.

    A clean exit used to signal nothing, which made "the harness returns 0 quickly" the
    cheapest way to leave a process behind. It signals now.

    ``pgid`` must be read before ``communicate`` reaps the child; afterwards
    ``os.getpgid(proc.pid)`` raises and the group the survivors are in is unaddressable.
    """
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
            return
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_SECONDS)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)


#: The server's end of the socketpair to the unprivileged harness runner, and a lock around
#: it. The protocol is one request, one response, so two concurrent runs would interleave
#: their frames; runs execute on worker threads, so this is a real possibility, not a
#: theoretical one.
_runner_lock = threading.Lock()
_runner_sock: socket.socket | None = None


def _runner() -> socket.socket | None:
    """The harness runner socket, or ``None`` when running without one.

    Absent under the tests, in dev mode, and whenever the container was started as a plain
    user rather than root -- in all of which the server executes harnesses in-process, as
    it did before uid separation. In a deployed image :mod:`ticket_to_pr.launch` always
    provides one.
    """
    global _runner_sock
    if _runner_sock is None:
        raw = os.environ.get(RUNNER_FD_ENV)
        if raw is None:
            return None
        _runner_sock = socket.socket(fileno=int(raw))
    return _runner_sock


def _run_via_runner(
    sock: socket.socket,
    argv: Sequence[str],
    *,
    repo_dir: Path,
    prompt: str,
    timeout_s: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Ask the unprivileged runner to execute the CLI, and map its answer to the contract.

    The runner owns the timeout: it holds the process group, so it is the only party that
    can take the whole tree down. A budget is added to the socket read purely so a wedged
    runner cannot hang a run forever.
    """
    request: dict[str, object] = {
        "argv": list(argv),
        "cwd": str(repo_dir),
        "env": env,
        "prompt": prompt,
        "timeout_s": timeout_s,
    }
    with _runner_lock:
        try:
            send_frame(sock, request)
            sock.settimeout(timeout_s + KILL_GRACE_SECONDS * 4)
            response = recv_frame(sock)
        except (OSError, ValueError) as exc:
            raise RunFailure(
                ErrorCode.HARNESS_FAILED, f"the harness runner became unreachable: {exc}"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                sock.settimeout(None)

    if response is None:
        raise RunFailure(ErrorCode.HARNESS_FAILED, "the harness runner exited unexpectedly")
    status = response.get("status")
    if status == "timeout":
        raise RunFailure(
            ErrorCode.TIMEOUT, f"the coding harness exceeded its {timeout_s}s budget"
        )
    if status == "start_failed":
        raise RunFailure(
            ErrorCode.HARNESS_FAILED,
            f"could not start {argv[0]!r}: {response.get('message', 'unknown error')}",
        )
    if status != "ok":
        raise RunFailure(
            ErrorCode.HARNESS_FAILED, f"the harness runner returned {status!r}"
        )
    exit_code = response.get("exit_code")
    return subprocess.CompletedProcess(
        list(argv),
        exit_code if isinstance(exit_code, int) else 1,
        str(response.get("stdout", "")),
        str(response.get("stderr", "")),
    )


def run_cli(
    argv: Sequence[str],
    *,
    repo_dir: Path,
    prompt: str,
    timeout_s: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run a harness CLI with the prompt on stdin, mapping timeout to the contract code.

    The prompt goes on stdin rather than argv: it is untrusted work-item text, it can be
    tens of kilobytes, and argv is world-readable.

    ``start_new_session=True`` puts the CLI in its own process group so a timeout can take
    the whole tree down; see :func:`_terminate_group`.
    """
    log.info("running harness: %s", " ".join(argv[:3]))
    sock = _runner()
    if sock is not None:
        return _run_via_runner(
            sock, argv, repo_dir=repo_dir, prompt=prompt, timeout_s=timeout_s, env=env
        )
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv built from validated settings
            list(argv),
            cwd=str(repo_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise RunFailure(
            ErrorCode.HARNESS_FAILED, f"could not start {argv[0]!r}: {exc.strerror}"
        ) from exc

    # Read while the child is still un-reaped; see _terminate_group.
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:  # pragma: no cover - the child died between Popen and here
        pgid = None

    try:
        with proc:
            try:
                stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                _terminate_group(proc, pgid)
                # Drain the pipes so the file descriptors close; the group is dead by now,
                # so this cannot block for long, but it is bounded anyway.
                with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
                    proc.communicate(timeout=KILL_GRACE_SECONDS)
                raise RunFailure(
                    ErrorCode.TIMEOUT, f"the coding harness exceeded its {timeout_s}s budget"
                ) from exc
        return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
    finally:
        # Success, failure and timeout alike. An already-empty group answers with
        # ProcessLookupError, which _terminate_group suppresses.
        _terminate_group(proc, pgid)


def require_cli(executable: str, harness_name: str) -> str:
    path = shutil.which(executable)
    if path is None:
        raise ConfigError(
            f"the {harness_name} harness needs {executable!r} on PATH but it was not found."
        )
    return path


def get_harness(name: str | HarnessName) -> Harness:
    """Factory (docs/architecture.md §4.2). Raises :class:`ConfigError` for unknown values."""
    try:
        harness_name = HarnessName(str(name))
    except ValueError:
        allowed = ", ".join(h.value for h in HarnessName)
        raise ConfigError(f"unknown harness {name!r}; expected one of: {allowed}") from None

    match harness_name:
        case HarnessName.CODEX:
            from .codex import CodexHarness

            return CodexHarness()
        case HarnessName.CLAUDE_CODE:
            from .claude_code import ClaudeCodeHarness

            return ClaudeCodeHarness()
        case HarnessName.FAKE:
            from .fake import FakeHarness

            return FakeHarness()


__all__ = [
    "ENV_ALLOWLIST",
    "FORBIDDEN_ENV_FRAGMENTS",
    "FOUNDRY_SCOPE",
    "Harness",
    "HarnessResult",
    "clean_env",
    "foundry_token",
    "get_harness",
    "harness_scratch_dir",
    "require_cli",
    "run_cli",
]


def foundry_endpoint() -> str:
    """The Foundry project endpoint the platform injects (docs/architecture.md §2).

    Never declared in ``azure.yaml`` (docs/configuration.md §2) -- it only ever arrives from the
    hosting platform, which is why it is read here rather than through ``Settings``.
    """
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    raise ConfigError(
        "FOUNDRY_PROJECT_ENDPOINT is not set. The Foundry platform injects it at "
        "runtime; if you are running outside Foundry, use the fake harness in dev mode."
    )
