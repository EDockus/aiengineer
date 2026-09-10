"""Orchestration: fetch -> branch -> code -> push -> PR (docs/architecture.md §3).

The invariant this module exists to hold is "no dangling remote branches": nothing is
pushed until the harness has succeeded and its output has passed the safety rails, and
if pull request creation fails after a push the branch is deleted again before the
failure is reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import ado, git_ops
from . import prompt as prompt_builder
from .config import Settings
from .harness import Harness, HarnessResult
from .models import (
    ErrorCode,
    InvocationRequest,
    Phase,
    Repository,
    RunError,
    RunFailure,
    RunResult,
    RunState,
    RunStatus,
    WorkItem,
)
from .redact import redact
from .runner import RUNNER_FD_ENV

log = logging.getLogger(__name__)

#: How much harness output may appear in an ``error.message`` on the wire.
DETAIL_CAP = 800

#: How much harness output is kept at all. Everything downstream (the pull request body,
#: the commit message, the error payload) is bounded further; this is the outer bound.
OUTPUT_CAP = 16_000


def _now() -> datetime:
    return datetime.now(UTC)


class Run:
    """Mutable state for one invocation. Owned by the event loop; not thread-shared."""

    def __init__(self, run_id: str, correlation_id: str | None) -> None:
        self.run_id = run_id
        self.correlation_id = correlation_id
        self.state = RunState.QUEUED
        self.phase = Phase.FETCHING_WORK_ITEM
        self.started_at = _now()
        self.finished_at: datetime | None = None
        self.result: RunResult | None = None
        self.error: RunError | None = None

    def set_phase(self, phase: Phase) -> None:
        self.state = RunState.RUNNING
        self.phase = phase
        log.info("run %s -> %s", self.run_id, phase.value)

    def succeed(self, result: RunResult) -> None:
        self.state = RunState.SUCCEEDED
        self.phase = Phase.DONE
        self.result = result
        self.finished_at = _now()
        log.info("run %s succeeded: PR %s", self.run_id, result.pull_request_id)

    def fail(self, code: ErrorCode, message: str) -> None:
        self.state = RunState.FAILED
        self.error = RunError(code=code, message=message)
        self.finished_at = _now()
        log.warning("run %s failed in %s: %s: %s", self.run_id, self.phase.value, code, message)

    def status(self) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            status=self.state,
            phase=self.phase,
            started_at=self.started_at,
            finished_at=self.finished_at,
            result=self.result,
            error=self.error,
            correlation_id=self.correlation_id,
        )


class RunStore:
    """In-memory run registry.

    One process holds the runs, which is why background mode plus a single replica is the
    supported topology (docs/architecture.md §2). Completed runs are evicted oldest-first so a
    long-lived container does not grow without bound.
    """

    def __init__(self, capacity: int = 500) -> None:
        self._runs: dict[str, Run] = {}
        self._capacity = capacity

    def create(self, correlation_id: str | None, run_id: str | None = None) -> Run:
        """Start a run.

        ``run_id`` lets the caller supply the hosting platform's own invocation id. The
        Foundry gateway validates the id format on ``GET /invocations/{id}`` and rejects
        anything that is not one of its own, so a run keyed by a locally-generated UUID is
        unreachable through the gateway. Falls back to a UUID when nothing is supplied --
        local development, the dev stub, and the tests all take that path.

        **An id already in use is refused, not reused.** ``GET /invocations/{id}`` has no
        ownership check -- it cannot have one, the platform authorizes the caller before
        the request arrives and this process never sees who that was -- so overwriting an
        existing key would hand the first caller the *second* caller's status, correlation
        id and error message on their next poll. Colliding on a fresh UUID instead costs
        that one invocation its gateway correlation and nothing else. It is validated
        upstream (``main.RUN_ID_RE``), so a collision here means a genuinely repeated id.
        """
        if run_id is not None and run_id in self._runs:
            log.warning(
                "invocation id already in use by a run started at %s; keying this run by "
                "a generated id instead. It will not be pollable through the gateway.",
                self._runs[run_id].started_at.isoformat(),
            )
            run_id = None
        run = Run(run_id or str(uuid.uuid4()), correlation_id)
        self._runs[run.run_id] = run
        self._evict()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def _evict(self) -> None:
        while len(self._runs) > self._capacity:
            for run_id, run in self._runs.items():
                if run.finished_at is not None:
                    del self._runs[run_id]
                    break
            else:
                return


def _make_run_dir(run_id: str) -> Path:
    """Create the per-run working directory, shared with the harness uid when there is one.

    The harness runs as its own uid (see :mod:`ticket_to_pr.runner`), so it cannot write
    into a directory owned 0700 by the server -- and the server has to be able to read the
    result back, commit it and delete it. Both accounts sit in one shared group, and the
    directory is created 2770 in that group: the setgid bit makes everything the harness
    creates inherit the group, and the runner's 002 umask makes it group-writable.

    Without a runner (tests, dev mode, non-root container) nothing is shared and the
    directory keeps mkdtemp's private 0700.
    """
    # Belt and braces on top of main.RUN_ID_RE. The id can originate in a request header,
    # and mkdtemp interprets its prefix as a path fragment: a `../` in one would place the
    # run directory outside TMPDIR. The fixed `t2pr-` prefix already makes traversal raise
    # rather than escape, but a validator one module away is not a defence this line should
    # be leaning on -- so the slice keeps only characters that cannot mean anything to a
    # path, and falls back to a literal when nothing survives.
    stem = "".join(c for c in run_id[:8] if c.isalnum() or c in "._-") or "run"
    work_dir = Path(tempfile.mkdtemp(prefix=f"t2pr-{stem}-"))
    raw_gid = os.environ.get("T2PR_SHARED_GID")
    if raw_gid is None or RUNNER_FD_ENV not in os.environ:
        return work_dir
    try:
        os.chown(work_dir, -1, int(raw_gid))
        # Group-write is the point: the shared group has exactly two members, the server
        # and the harness runner, and this directory is deleted at the end of the run.
        # World access stays denied.
        work_dir.chmod(0o2770)
    except (OSError, ValueError) as exc:
        # Not fatal on its own, but the harness will fail to write, so say why loudly.
        log.warning("could not share the run directory with the harness group: %s", exc)
    return work_dir


class Orchestrator:
    def __init__(self, cfg: Settings, harness: Harness, tokens: ado.TokenProvider) -> None:
        self._cfg = cfg
        self._harness = harness
        self._tokens = tokens
        #: Branch deletions still in flight. Kept so a cleanup started while the run was
        #: being cancelled is not garbage-collected before it finishes; ``drain`` lets
        #: shutdown wait for them (docs/architecture.md §3).
        self._cleanups: set[asyncio.Task[None]] = set()

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for any in-flight branch cleanup. Called from the app's shutdown path."""
        pending = list(self._cleanups)
        if not pending:
            return
        log.info("waiting for %d branch cleanup(s) to finish", len(pending))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)

    async def execute(self, run: Run, request: InvocationRequest) -> None:
        """Run the full sequence, recording phase and terminal state on ``run``."""
        work_dir = _make_run_dir(run.run_id)
        repo_dir = work_dir / "repo"
        try:
            await self._execute(run, request, repo_dir)
        except RunFailure as failure:
            run.fail(failure.code, failure.message)
        except asyncio.CancelledError:
            run.fail(ErrorCode.INTERNAL, "the run was cancelled")
            raise
        except Exception:
            # Anything reaching here is a bug, not a mapped failure. Log the traceback but
            # do not leak its text -- it can contain work item content.
            log.exception("run %s raised an unmapped exception", run.run_id)
            run.fail(ErrorCode.INTERNAL, "an unexpected internal error occurred")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _execute(self, run: Run, request: InvocationRequest, repo_dir: Path) -> None:
        cfg = self._cfg

        # 1. Allowlist.
        if not cfg.org_allowed(request.organization):
            raise RunFailure(
                ErrorCode.ADO_FORBIDDEN,
                f"organization {request.organization!r} is not in T2PR_ALLOWED_ORGS",
            )

        client = ado.AdoClient(cfg=cfg, organization=request.organization, tokens=self._tokens)
        pushed = False
        pr_outcome_unknown = False
        head_sha = ""
        repo = None
        try:
            # 2. Work item.
            run.set_phase(Phase.FETCHING_WORK_ITEM)
            known_types = await client.list_work_item_types(request.project)
            if known_types and request.work_item.type not in known_types:
                raise RunFailure(
                    ErrorCode.WORK_ITEM_NOT_FOUND,
                    f"work item type {request.work_item.type!r} is not defined in project "
                    f"{request.project!r}",
                )
            work_item = await client.get_work_item(request.project, request.work_item.id)
            log.info("run %s fetched work item #%s", run.run_id, work_item.id)

            # 3. Repository and branches.
            repo = await client.get_repository(request.project, request.repository)
            base_branch = request.base_branch or repo.default_branch
            if await client.get_branch_object_id(request.project, repo.id, request.branch):
                raise RunFailure(
                    ErrorCode.BRANCH_EXISTS, f"branch {request.branch!r} already exists"
                )
            if not await client.get_branch_object_id(request.project, repo.id, base_branch):
                raise RunFailure(
                    ErrorCode.REPOSITORY_NOT_FOUND,
                    f"base branch {base_branch!r} does not exist in {repo.name!r}",
                )

            # 4-5. Clone and branch.
            run.set_phase(Phase.CLONING)
            token = await self._tokens.token()
            await asyncio.to_thread(
                git_ops.clone,
                remote_url=repo.remote_url,
                base_branch=base_branch,
                dest=repo_dir,
                token=token,
                scheme=self._tokens.scheme.lower(),
            )
            await asyncio.to_thread(git_ops.create_branch, repo_dir, request.branch)

            # 6. Code.
            run.set_phase(Phase.CODING)
            await asyncio.to_thread(git_ops.strip_credential_config, repo_dir)
            text = prompt_builder.build_prompt(work_item)
            result = _sanitise(
                await asyncio.to_thread(self._harness.run, repo_dir, text, cfg.max_run_seconds)
            )
            # Again, now that the harness has had write access to the run directory. The
            # call before it covers a repository that arrived armed; this one covers the
            # harness arming it -- a planted ``.git/hooks/pre-push`` executed as the server
            # uid with the Azure DevOps PAT in its environment, which is the whole point of
            # keeping the PAT out of the harness environment in the first place.
            await asyncio.to_thread(git_ops.strip_credential_config, repo_dir)
            _check_harness(result)

            # 7. Safety rails.
            changed = await asyncio.to_thread(git_ops.changed_files, repo_dir)
            if not changed:
                raise RunFailure(
                    ErrorCode.HARNESS_NO_CHANGES, "the harness did not modify any files"
                )
            if len(changed) > cfg.max_files_changed:
                raise RunFailure(
                    ErrorCode.HARNESS_FAILED,
                    f"the harness changed {len(changed)} files, over the "
                    f"T2PR_MAX_FILES_CHANGED limit of {cfg.max_files_changed}",
                )

            # 8. Commit and push.
            await asyncio.to_thread(
                git_ops.commit_all,
                repo_dir,
                message=prompt_builder.commit_message(work_item, summary=result.summary),
                author_name=cfg.commit_author_name,
                author_email=cfg.commit_author_email,
            )
            run.set_phase(Phase.PUSHING)
            token = await self._tokens.token()
            await asyncio.to_thread(
                git_ops.push,
                repo_dir,
                branch=request.branch,
                token=token,
                scheme=self._tokens.scheme.lower(),
            )
            pushed = True
            head_sha = await asyncio.to_thread(git_ops.head_object_id, repo_dir)
            commits = await asyncio.to_thread(git_ops.commit_count, repo_dir, base_branch)

            # 9. Pull request.
            run.set_phase(Phase.CREATING_PR)
            try:
                pr_id, pr_url = await client.create_pull_request(
                    project=request.project,
                    repo=repo,
                    source_branch=request.branch,
                    target_branch=base_branch,
                    title=prompt_builder.pr_title(work_item),
                    description=prompt_builder.pr_description(
                        work_item, summary=result.summary, files=changed
                    ),
                    draft=cfg.pr_draft,
                    reviewers=cfg.pr_reviewers,
                )
            except RunFailure as failure:
                # A timeout here can mean the pull request *was* created; the cleanup then
                # has to look before it deletes.
                pr_outcome_unknown = failure.ambiguous
                raise
            pushed = False  # The branch now belongs to a PR; leave it alone.

            # 10. Done.
            run.succeed(
                RunResult(
                    pull_request_id=pr_id,
                    pull_request_url=pr_url,
                    branch=request.branch,
                    commits=commits,
                    files_changed=len(changed),
                )
            )
        finally:
            try:
                if pushed and repo is not None:
                    await self._cleanup_uncancellable(
                        client, request, repo, head_sha, verify=pr_outcome_unknown
                    )
            finally:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(asyncio.ensure_future(client.aclose()))

    async def _cleanup_uncancellable(
        self,
        client: ado.AdoClient,
        request: InvocationRequest,
        repo: Repository,
        head_sha: str,
        *,
        verify: bool,
    ) -> None:
        """Start the cleanup as a shielded task so cancellation cannot skip it.

        Awaiting directly in a ``finally`` re-raises :class:`asyncio.CancelledError` at the
        first suspension point, which meant a shutdown mid-run left the branch behind --
        exactly what docs/architecture.md §3 forbids. The task outlives this coroutine and
        ``drain`` waits for it.
        """
        task = asyncio.ensure_future(
            self._cleanup_branch(client, request, repo, head_sha, verify=verify)
        )
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    async def _cleanup_branch(
        self,
        client: ado.AdoClient,
        request: InvocationRequest,
        repo: Repository,
        head_sha: str,
        *,
        verify: bool,
    ) -> None:
        """Undo a push whose pull request never got created (docs/architecture.md §3).

        ``verify`` is set when pull request creation failed *ambiguously* -- a timeout on
        the response, say -- because Azure DevOps may have created one anyway, and deleting
        the source branch of a live pull request is worse than leaving a branch behind. A
        definite rejection (400, 409, 500) needs no such check, so the ordinary path costs
        no extra call. If the check itself cannot be answered, the branch stays.
        """
        if verify:
            try:
                existing = await client.find_pull_request(
                    request.project, repo.id, request.branch
                )
            except RunFailure as exc:
                log.warning(
                    "run cleanup: could not check for a pull request on %s (%s); leaving "
                    "the branch in place rather than risk deleting a live one",
                    request.branch,
                    exc.message,
                )
                return
            if existing is not None:
                log.warning(
                    "run cleanup: leaving branch %s alone, pull request %d exists on it",
                    request.branch,
                    existing,
                )
                return
        log.warning(
            "run cleanup: deleting remote branch %s after a failure post-push", request.branch
        )
        with contextlib.suppress(RunFailure):
            await client.delete_branch(request.project, repo.id, request.branch, head_sha)


def _sanitise(result: HarnessResult) -> HarnessResult:
    """Redact and bound harness output at the single point it enters the orchestrator.

    Everything the harness produced is untrusted: it is model output driven by a work-item
    description that anyone able to file a ticket controls, and it reaches a pull request
    body, a commit message and ``error.message`` on the wire. Redacting only git's stderr
    (which is what :mod:`ticket_to_pr.git_ops` used to do alone) left all three unguarded.
    """
    return HarnessResult(
        exit_code=result.exit_code,
        summary=redact(result.summary)[:OUTPUT_CAP],
        stdout=redact(result.stdout)[-OUTPUT_CAP:],
        stderr=redact(result.stderr)[-OUTPUT_CAP:],
    )


def _check_harness(result: HarnessResult) -> None:
    if result.exit_code == 0:
        return
    # Already redacted by _sanitise; redacted again because this is the last thing between
    # harness output and the wire, and a future caller may not have gone through _sanitise.
    detail = redact(result.stderr or result.stdout).strip()[-DETAIL_CAP:]
    raise RunFailure(
        ErrorCode.HARNESS_FAILED,
        f"the coding harness exited with code {result.exit_code}: {detail}",
    )


__all__ = ["Orchestrator", "Run", "RunStore", "WorkItem"]
