"""The end-to-end contract test (docs/architecture.md §7) and the orchestration invariants.

``run.py`` executes its whole sequence here: recorded Azure DevOps fixtures answer every
REST call through the real ``AdoClient``, a real bare repository on disk answers every
git call through the real ``git_ops``, and ``FakeHarness`` writes one file. Nothing is
stubbed inside the agent.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from ticket_to_pr import run as run_module
from ticket_to_pr.config import Settings
from ticket_to_pr.harness.fake import MARKER_FILE, FakeHarness
from ticket_to_pr.models import ErrorCode, InvocationRequest, Phase, RunFailure, RunState
from ticket_to_pr.run import Orchestrator, Run, RunStore

from .conftest import (
    DEFAULT_BRANCH,
    TARGET_BRANCH,
    ZERO,
    FakeAdo,
    ScriptedHarness,
    git,
    make_request,
    make_settings,
    remote_branches,
    request_body,
)


class Tokens:
    """A token provider that hands back a value tests can look for."""

    VALUE = "test-ado-token-value"

    #: Matches the TokenProvider protocol. Production uses Basic (a PAT); the value here
    #: is opaque, so the scheme only has to exist and be echoed into the header.
    scheme = "Basic"

    def __init__(self) -> None:
        self.calls = 0

    async def token(self) -> str:
        self.calls += 1
        return self.VALUE

    async def aclose(self) -> None:
        return None


async def execute(
    harness: Any,
    fake_ado: FakeAdo,
    *,
    settings: Settings | None = None,
    **request_overrides: Any,
) -> Run:
    cfg = settings or make_settings()
    run = RunStore().create("test-correlation")
    orchestrator = Orchestrator(cfg, harness, Tokens())
    await orchestrator.execute(run, make_request(**request_overrides))
    return run


# ============================================================== the happy-path contract test


async def test_end_to_end_produces_a_pull_request(fake_ado: FakeAdo, bare_repo: Path) -> None:
    """docs/architecture.md §7: run.py end to end with a fake harness that writes one file."""
    run = await execute(FakeHarness(), fake_ado)

    assert run.state is RunState.SUCCEEDED, run.error
    assert run.phase is Phase.DONE
    assert run.error is None
    assert run.result is not None
    assert run.result.pull_request_id == 812
    assert run.result.pull_request_url.endswith("/pullrequest/812")
    assert run.result.branch == TARGET_BRANCH
    assert run.result.commits == 1
    assert run.result.files_changed == 1
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert run.correlation_id == "test-correlation"

    # The branch really exists on the remote, with the file the harness wrote.
    branches = remote_branches(bare_repo)
    assert f"refs/heads/{TARGET_BRANCH}" in branches
    listing = git(["ls-tree", "--name-only", "-r", TARGET_BRANCH], cwd=bare_repo)
    assert MARKER_FILE in listing.split()


async def test_the_status_body_serialises_to_the_documented_shape(fake_ado: FakeAdo) -> None:
    run = await execute(FakeHarness(), fake_ado)
    body = json.loads(run.status().model_dump_json())
    assert set(body) == {
        "runId", "status", "phase", "startedAt", "finishedAt", "result", "error",
        "correlationId",
    }
    assert set(body["result"]) == {
        "pullRequestId", "pullRequestUrl", "branch", "commits", "filesChanged"
    }
    assert body["status"] == "succeeded"
    assert body["phase"] == "done"
    assert body["error"] is None


async def test_the_pull_request_body_matches_the_spec(fake_ado: FakeAdo) -> None:
    settings = make_settings(pr_draft=True, pr_reviewers=("reviewer-guid",))
    await execute(FakeHarness(), fake_ado, settings=settings)
    posts = fake_ado.sent("POST", "/pullrequests")
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["sourceRefName"] == f"refs/heads/{TARGET_BRANCH}"
    assert body["targetRefName"] == f"refs/heads/{DEFAULT_BRANCH}"
    assert body["title"].startswith("Bug #4123: ")
    assert body["isDraft"] is True
    assert body["reviewers"] == [{"id": "reviewer-guid"}]
    assert "Fixes AB#4123" in body["description"]
    assert "## What changed" in body["description"]
    assert "## How to test" in body["description"]


async def test_draft_false_is_honoured(fake_ado: FakeAdo) -> None:
    await execute(FakeHarness(), fake_ado, settings=make_settings(pr_draft=False))
    body = json.loads(fake_ado.sent("POST", "/pullrequests")[0].content)
    assert body["isDraft"] is False


async def test_the_base_branch_defaults_to_the_repository_default(fake_ado: FakeAdo) -> None:
    body = request_body()
    del body["baseBranch"]
    run = RunStore().create(None)
    await Orchestrator(make_settings(), FakeHarness(), Tokens()).execute(
        run, InvocationRequest.model_validate(body)
    )
    assert run.state is RunState.SUCCEEDED, run.error
    pr = json.loads(fake_ado.sent("POST", "/pullrequests")[0].content)
    assert pr["targetRefName"] == "refs/heads/main"  # from the recorded repository fixture


async def test_every_ado_call_carries_the_providers_credential(fake_ado: FakeAdo) -> None:
    """Auth on every call, in whatever scheme the provider declares.

    Azure DevOps takes a PAT as HTTP Basic, not Bearer (docs/deploy.md §3), so the
    scheme comes from the token provider rather than being hard-coded in the client.
    """
    await execute(FakeHarness(), fake_ado)
    assert fake_ado.auth_headers
    for header in fake_ado.auth_headers:
        assert header == f"{Tokens.scheme} {Tokens.VALUE}"


async def test_the_prompt_reaches_the_harness_with_the_work_item_content(
    fake_ado: FakeAdo,
) -> None:
    harness = ScriptedHarness()
    await execute(harness, fake_ado)
    (_repo_dir, prompt, timeout_s) = harness.calls[0]
    assert "work item #4123 (Bug)" in prompt
    assert "Null reference when charging a zero-amount order" in prompt
    # HTML from the recorded ADO fixture is stripped before it reaches the model.
    assert "<b>" not in prompt and "<div>" not in prompt
    assert "NullReferenceException" in prompt
    # The linked work item title came from a second recorded fetch.
    assert "#4100 Payments hardening" in prompt
    assert timeout_s == make_settings().max_run_seconds


async def test_the_harness_timeout_budget_comes_from_config(fake_ado: FakeAdo) -> None:
    """T2PR_MAX_RUN_MINUTES is enforced by being handed to the harness, in seconds."""
    harness = ScriptedHarness()
    await execute(harness, fake_ado, settings=make_settings(max_run_minutes=3))
    assert harness.calls[0][2] == 180


async def test_credential_config_is_stripped_before_the_harness_runs(
    fake_ado: FakeAdo,
) -> None:
    """docs/architecture.md §4.2, observed from inside the harness itself."""
    seen: dict[str, str] = {}

    def inspect(repo_dir: Path) -> None:
        seen["config"] = (repo_dir / ".git" / "config").read_text(encoding="utf-8")

    await execute(ScriptedHarness(side_effect=inspect), fake_ado)
    assert "extraheader" not in seen["config"].casefold()
    assert "helper" not in seen["config"].casefold()
    assert Tokens.VALUE not in seen["config"]


# ------------------------------------------------ what the harness leaves behind in .git/


HOOK_NAMES = (
    "pre-push",
    "pre-commit",
    "commit-msg",
    "prepare-commit-msg",
    "post-commit",
    "reference-transaction",
    "post-index-change",
)


def _arming_harness(evidence: Path, *, via_hooks_path: bool = False) -> ScriptedHarness:
    """A harness that behaves itself and arms ``.git/`` on its way out.

    This is the shape of the attack the uid split and the environment allowlist do not
    touch: it never reads its own environment, it gets the *server* to hand it one. The
    run directory is shared with the harness by design, so ``.git/hooks/`` and
    ``.git/config`` are as writable to it as the source files are.
    """

    def arm(repo_dir: Path) -> None:
        evidence.mkdir(parents=True, exist_ok=True)
        hooks = repo_dir / ".git" / "hooks"
        if via_hooks_path:
            hooks = repo_dir.parent / "hooks-elsewhere"
            git(["config", "core.hooksPath", str(hooks)], cwd=repo_dir)
        hooks.mkdir(parents=True, exist_ok=True)
        for name in HOOK_NAMES:
            hook = hooks / name
            hook.write_text(
                f'#!/bin/sh\nenv > "{evidence}/{name}.env"\nexit 0\n', encoding="utf-8"
            )
            hook.chmod(0o755)

    return ScriptedHarness(side_effect=arm)


async def test_a_harness_planted_hook_never_executes(
    fake_ado: FakeAdo, tmp_path: Path
) -> None:
    """The finding, end to end.

    Before the fix the ``pre-push`` hook ran as the server uid with the Azure DevOps
    credential in its environment as ``GIT_CONFIG_VALUE_0``: verbatim PAT *and* code
    execution as the uid holding it, bypassing the uid split, the environment allowlist and
    the launcher scrub at once. The run must still succeed -- the defence is not "refuse
    the run", it is "git never runs what the harness left".
    """
    evidence = tmp_path / "hooks-that-ran"
    run = await execute(_arming_harness(evidence), fake_ado)

    assert run.state is RunState.SUCCEEDED, run.error
    ran = sorted(path.name for path in evidence.iterdir())
    assert ran == [], f"git executed harness-planted hooks: {ran}"


async def test_a_harness_planted_hooks_path_redirect_never_executes(
    fake_ado: FakeAdo, tmp_path: Path
) -> None:
    """Emptying ``.git/hooks/`` misses this one: ``core.hooksPath`` points git elsewhere."""
    evidence = tmp_path / "redirected-hooks-that-ran"
    run = await execute(_arming_harness(evidence, via_hooks_path=True), fake_ado)

    assert run.state is RunState.SUCCEEDED, run.error
    ran = sorted(path.name for path in evidence.iterdir())
    assert ran == [], f"core.hooksPath redirected git to harness-owned hooks: {ran}"


async def test_credential_config_is_stripped_again_after_the_harness_runs(
    fake_ado: FakeAdo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripping only *before* the harness protects against the wrong party.

    The harness is the one with write access to ``.git/`` during the run, so the
    interesting rewrite happens after the pre-harness strip has already been and gone.

    Observed from ``changed_files``, the first git call after the harness returns, because
    the run directory is deleted before the test regains control.
    """
    seen: dict[str, str] = {}

    def arm(repo_dir: Path) -> None:
        config = repo_dir / ".git" / "config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[credential]\n\thelper = !cat /proc/self/environ\n"
            + '[http "https://dev.azure.com"]\n\textraheader = AUTHORIZATION: basic leak\n'
            + '[filter "evil"]\n\tclean = /tmp/evil.sh\n'
            + "[core]\n\tfsmonitor = /tmp/evil.sh\n\thooksPath = /tmp/evil-hooks\n",
            encoding="utf-8",
        )

    real = run_module.git_ops.changed_files

    def snapshot(repo_dir: Path) -> list[str]:
        seen["config"] = (repo_dir / ".git" / "config").read_text(encoding="utf-8")
        return real(repo_dir)

    monkeypatch.setattr(run_module.git_ops, "changed_files", snapshot)
    run = await execute(ScriptedHarness(side_effect=arm), fake_ado)

    assert run.state is RunState.SUCCEEDED, run.error
    after = seen["config"].casefold()
    for key in ("helper", "extraheader", "clean", "fsmonitor", "hookspath"):
        assert key not in after, f"{key!r} survived the post-harness strip"
    assert "leak" not in after


# ================================================================== the no-dangling-branch rule


async def test_nothing_is_pushed_when_the_harness_fails(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    """docs/architecture.md §3: "nothing is pushed until the harness has succeeded"."""
    run = await execute(ScriptedHarness(exit_code=1, summary="boom"), fake_ado)
    assert run.error is not None and run.error.code is ErrorCode.HARNESS_FAILED
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)
    assert fake_ado.sent("POST", "/refs") == []


async def test_nothing_is_pushed_when_the_harness_makes_no_changes(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    run = await execute(ScriptedHarness(files=0), fake_ado)
    assert run.error is not None and run.error.code is ErrorCode.HARNESS_NO_CHANGES
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


async def test_pr_failure_after_a_successful_push_deletes_the_remote_branch(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    """The invariant from docs/architecture.md §3, exercised on the post-push path.

    The push genuinely succeeds -- the branch is on the remote at the moment pull request
    creation is refused -- so this is the real cleanup path, not the "source ref was never
    pushed" shortcut a dev stub takes.
    """
    fake_ado.force["pull_request"] = 500
    run = await execute(FakeHarness(), fake_ado)

    assert run.state is RunState.FAILED
    assert run.error is not None
    assert run.phase is Phase.CREATING_PR

    # 1. The push really happened before the PR call.
    methods = [(r.method, str(r.url)) for r in fake_ado.requests]
    pr_index = next(i for i, (m, u) in enumerate(methods) if m == "POST" and "pullrequests" in u)
    assert pr_index >= 0

    # 2. The branch is gone from the remote again.
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)

    # 3. It was deleted by the documented ref update, after the PR attempt.
    deletes = fake_ado.sent("POST", "/refs")
    assert len(deletes) == 1
    payload = json.loads(deletes[0].content)
    assert payload[0]["name"] == f"refs/heads/{TARGET_BRANCH}"
    assert payload[0]["newObjectId"] == ZERO
    assert payload[0]["oldObjectId"] != ZERO and len(payload[0]["oldObjectId"]) == 40
    delete_index = methods.index(("POST", str(deletes[0].url)))
    assert delete_index > pr_index


async def test_the_branch_really_was_on_the_remote_before_cleanup_ran(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    """Guards the test above from passing vacuously if the push had silently failed."""
    observed: dict[str, Any] = {}
    original = FakeAdo._create_pr

    def spy(self: FakeAdo, request: Any) -> Any:
        observed["branches"] = remote_branches(self.bare)
        return original(self, request)

    FakeAdo._create_pr = spy  # type: ignore[method-assign]
    try:
        fake_ado.force["pull_request"] = 500
        await execute(FakeHarness(), fake_ado)
    finally:
        FakeAdo._create_pr = original  # type: ignore[method-assign]

    assert f"refs/heads/{TARGET_BRANCH}" in observed["branches"]
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


async def test_cleanup_still_reports_the_original_failure_if_deletion_also_fails(
    fake_ado: FakeAdo,
) -> None:
    """A failed cleanup must not mask, or replace, the failure that caused it."""
    fake_ado.force["pull_request"] = 500
    fake_ado.force["update_refs"] = 403
    run = await execute(FakeHarness(), fake_ado)
    assert run.state is RunState.FAILED
    assert run.error is not None
    assert run.error.code is not ErrorCode.ADO_FORBIDDEN  # not the cleanup's error


async def test_a_successful_run_never_deletes_its_branch(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    await execute(FakeHarness(), fake_ado)
    assert fake_ado.sent("POST", "/refs") == []
    assert f"refs/heads/{TARGET_BRANCH}" in remote_branches(bare_repo)


async def test_a_push_failure_leaves_no_branch_and_does_not_attempt_a_delete(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    hook = bare_repo / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    run = await execute(FakeHarness(), fake_ado)
    assert run.error is not None and run.error.code is ErrorCode.PUSH_FAILED
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)
    assert fake_ado.sent("POST", "/refs") == []


# ========================================================================= temp-dir cleanup


@pytest.fixture
def recorded_tempdirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    created: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args: Any, **kwargs: Any) -> str:
        path = real(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(run_module.tempfile, "mkdtemp", spy)
    return created


async def test_the_temp_dir_is_removed_after_a_successful_run(
    fake_ado: FakeAdo, recorded_tempdirs: list[Path]
) -> None:
    run = await execute(FakeHarness(), fake_ado)
    assert run.state is RunState.SUCCEEDED
    assert recorded_tempdirs and not recorded_tempdirs[0].exists()


@pytest.mark.parametrize(
    "harness_kwargs",
    [
        {"exit_code": 1},                       # harness_failed
        {"files": 0},                           # harness_no_changes
        {"raises": RunFailure(ErrorCode.TIMEOUT, "took too long")},
        {"raises": ValueError("an unmapped bug")},
    ],
)
async def test_the_temp_dir_is_removed_on_every_failing_exit_path(
    fake_ado: FakeAdo, recorded_tempdirs: list[Path], harness_kwargs: dict[str, Any]
) -> None:
    run = await execute(ScriptedHarness(**harness_kwargs), fake_ado)
    assert run.state is RunState.FAILED
    assert recorded_tempdirs and not recorded_tempdirs[0].exists()


async def test_the_temp_dir_is_removed_when_the_run_is_cancelled(
    fake_ado: FakeAdo, recorded_tempdirs: list[Path]
) -> None:
    started = asyncio.Event()

    def block(_repo_dir: Path) -> None:
        started.set()
        import time

        time.sleep(5)

    run = RunStore().create(None)
    orchestrator = Orchestrator(make_settings(), ScriptedHarness(side_effect=block), Tokens())
    task = asyncio.create_task(orchestrator.execute(run, make_request()))
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded_tempdirs and not recorded_tempdirs[0].exists()
    assert run.error is not None and run.error.code is ErrorCode.INTERNAL


async def test_cleanup_survives_a_temp_dir_that_was_already_removed(
    fake_ado: FakeAdo, recorded_tempdirs: list[Path]
) -> None:
    def nuke(repo_dir: Path) -> None:
        shutil.rmtree(repo_dir.parent)

    run = await execute(ScriptedHarness(side_effect=nuke), fake_ado)
    assert run.state is RunState.FAILED  # git status against a deleted tree
    assert not recorded_tempdirs[0].exists()


# ============================================================================== safety rails


async def test_exceeding_max_files_changed_fails_with_harness_failed(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    """docs/configuration.md §1: the rail is enforced, and nothing is pushed when it trips."""
    run = await execute(
        ScriptedHarness(files=4), fake_ado, settings=make_settings(max_files_changed=3)
    )
    assert run.error is not None
    assert run.error.code is ErrorCode.HARNESS_FAILED
    assert "T2PR_MAX_FILES_CHANGED limit of 3" in run.error.message
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


async def test_exactly_the_limit_is_allowed(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(files=3), fake_ado, settings=make_settings(max_files_changed=3)
    )
    assert run.state is RunState.SUCCEEDED, run.error
    assert run.result is not None and run.result.files_changed == 3


async def test_the_harness_timeout_maps_to_the_timeout_code(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(raises=RunFailure(ErrorCode.TIMEOUT, "exceeded its 60s budget")),
        fake_ado,
    )
    assert run.error is not None and run.error.code is ErrorCode.TIMEOUT


# ================================================================================ run store


def test_run_store_creates_unique_ids_and_finds_them() -> None:
    store = RunStore()
    a = store.create("c1")
    b = store.create(None)
    assert a.run_id != b.run_id
    assert store.get(a.run_id) is a
    assert store.get("nope") is None


def test_run_store_adopts_a_supplied_run_id() -> None:
    """Why the parameter exists: the Foundry gateway rejects any id but its own on GET."""
    store = RunStore()
    run = store.create("c1", run_id="7f3a1c20-0000-4000-8000-abcdefabcdef")
    assert run.run_id == "7f3a1c20-0000-4000-8000-abcdefabcdef"
    assert store.get("7f3a1c20-0000-4000-8000-abcdefabcdef") is run


def test_run_store_refuses_to_overwrite_an_id_already_in_use() -> None:
    """A second run may not take the first run's key.

    ``GET /invocations/{id}`` has no ownership check -- it cannot have one, the platform
    authorizes the caller before the request arrives -- so overwriting would hand the first
    caller the second caller's status, correlation id and error message on their next poll.
    Both runs survive; the second simply loses its gateway correlation.
    """
    store = RunStore()
    first = store.create("first", run_id="collide")
    second = store.create("second", run_id="collide")

    assert second.run_id != "collide"
    assert store.get("collide") is first
    assert store.get(second.run_id) is second
    assert first.correlation_id == "first"
    assert store.get("collide").correlation_id == "first"


def test_a_generated_id_is_still_minted_when_none_is_supplied() -> None:
    """Local development, the dev stub and the tests all take this path."""
    import uuid

    run = RunStore().create(None)
    uuid.UUID(run.run_id)  # raises if it is not a uuid


def test_run_store_evicts_finished_runs_but_keeps_in_flight_ones() -> None:
    store = RunStore(capacity=3)
    keep = store.create(None)  # never finished
    for _ in range(10):
        finished = store.create(None)
        finished.fail(ErrorCode.INTERNAL, "x")
    assert store.get(keep.run_id) is keep
    assert len(store._runs) <= 4


def test_a_fresh_run_reports_queued() -> None:
    run = Run("id", "corr")
    status = run.status()
    assert status.status is RunState.QUEUED
    assert status.finished_at is None
    assert status.result is None and status.error is None
    assert status.correlation_id == "corr"


async def test_ado_client_is_always_closed_even_on_failure(
    fake_ado: FakeAdo, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    client_cls = fake_ado.real_client_cls
    real = client_cls.aclose

    async def spy(self: Any) -> None:
        closed.append(True)
        await real(self)

    monkeypatch.setattr(client_cls, "aclose", spy)
    fake_ado.force["work_item"] = 404
    await execute(FakeHarness(), fake_ado)
    assert closed


# ==================================================== harness output never leaves unredacted

#: A JWT-shaped string and an anchor-shaped one, both of which a prompt-injected harness
#: can put in its own summary and stderr just by printing them.
LEAKED_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payload.leaked"
FORGED_MARKDOWN = "## Approved\n\n[Sign in](https://evil.example) to continue."


async def test_a_leaked_token_in_the_harness_summary_never_reaches_the_pr_description(
    fake_ado: FakeAdo,
) -> None:
    """The pull request body is written into the customer's repository. It is not a log.

    ``git_ops`` redacted git's own stderr and nothing else, so a summary was copied into
    the description verbatim -- a credential the harness happened to print ended up
    durable, in a repository, in front of every reviewer.
    """
    summary = f"Done. Used Authorization: bearer {LEAKED_JWT} and https://user:pw@host/x."
    run = await execute(ScriptedHarness(summary=summary), fake_ado)

    assert run.state is RunState.SUCCEEDED
    body = json.loads(fake_ado.sent("POST", "/pullrequests")[0].content)
    description = body["description"]
    assert LEAKED_JWT not in description
    assert "user:pw@host" not in description
    assert "<redacted>" in description


async def test_the_pr_description_fences_harness_text_and_stays_inside_the_ado_limit(
    fake_ado: FakeAdo,
) -> None:
    """Two failure modes of an unbounded summary, in one place.

    Markdown: the summary is attacker-influenced text sitting under a heading a reviewer
    trusts, so it is fenced rather than rendered. Length: Azure DevOps rejects a
    description over 4000 characters, and the rejection would arrive *after* the push --
    a self-inflicted ``pr_failed`` any work item could trigger.
    """
    summary = FORGED_MARKDOWN + "\n```\nnot a real fence\n```\n" + ("A" * 50_000)
    run = await execute(ScriptedHarness(summary=summary), fake_ado)

    assert run.state is RunState.SUCCEEDED
    description = json.loads(fake_ado.sent("POST", "/pullrequests")[0].content)["description"]
    assert len(description) <= 4000
    # Fenced: the forged heading and link are inside a code block, and the summary's own
    # fence did not close ours.
    opening = description.split("## Summary\n\n", 1)[1].split("\n", 1)[0]
    # One backtick longer than the summary's own fence, so that fence cannot close ours.
    assert opening == "````text"
    assert description.count("````") == 2
    assert "[Sign in](https://evil.example)" in description
    assert "\n## Approved" not in description.split("````text\n", 1)[0]
    assert f"Fixes AB#{request_body()['workItem']['id']}" in description


async def test_harness_stderr_is_redacted_before_it_reaches_error_message(
    fake_ado: FakeAdo,
) -> None:
    """``error.message`` goes on the wire to whoever called the agent."""
    stderr = f"fatal: Authorization: bearer {LEAKED_JWT}"
    run = await execute(ScriptedHarness(exit_code=2, stderr=stderr), fake_ado)

    assert run.error is not None and run.error.code is ErrorCode.HARNESS_FAILED
    assert LEAKED_JWT not in run.error.message
    assert "<redacted>" in run.error.message
    # And nothing was pushed, so the leak has no second route out either.
    assert not fake_ado.sent("POST", "/refs")
