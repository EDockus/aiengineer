"""Every one of the eleven v1 error codes, each actually produced (docs/contract.md §6).

The set is closed. :func:`test_every_error_code_has_a_test_that_produces_it` fails if a
future code is added without a test, or if one of these paths stops reaching its code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ticket_to_pr.harness.fake import FakeHarness
from ticket_to_pr.models import ErrorCode, RunFailure, RunState

from .conftest import (
    DEFAULT_BRANCH,
    TARGET_BRANCH,
    FakeAdo,
    ScriptedHarness,
    make_settings,
    remote_branches,
)
from .test_run_contract import execute

#: Filled in by each test below; asserted exhaustive at the end of the module.
PRODUCED: set[ErrorCode] = set()


def record(run: Any, expected: ErrorCode) -> str:
    assert run.state is RunState.FAILED, f"expected a failure, got {run.state}"
    assert run.error is not None
    assert run.error.code is expected, f"expected {expected}, got {run.error.code}"
    assert run.error.message, "an error code without a message is not actionable"
    PRODUCED.add(expected)
    return run.error.message


# ------------------------------------------------------------------------- ado_forbidden


async def test_ado_forbidden_when_the_org_is_not_in_the_allowlist(fake_ado: FakeAdo) -> None:
    run = await execute(FakeHarness(), fake_ado, organization="fabrikam")
    message = record(run, ErrorCode.ADO_FORBIDDEN)
    assert "T2PR_ALLOWED_ORGS" in message
    # The allowlist is checked before anything is fetched.
    assert fake_ado.requests == []


async def test_ado_forbidden_when_the_allowlist_is_empty(fake_ado: FakeAdo) -> None:
    run = await execute(
        FakeHarness(), fake_ado, settings=make_settings(allowed_orgs=frozenset())
    )
    record(run, ErrorCode.ADO_FORBIDDEN)


@pytest.mark.parametrize("status", [401, 403])
async def test_ado_forbidden_when_azure_devops_refuses(fake_ado: FakeAdo, status: int) -> None:
    fake_ado.force["work_item_types"] = status
    run = await execute(FakeHarness(), fake_ado)
    message = record(run, ErrorCode.ADO_FORBIDDEN)
    assert str(status) in message


async def test_ado_forbidden_when_azure_devops_answers_with_a_sign_in_page(
    fake_ado: FakeAdo,
) -> None:
    """An unauthenticated ADO request gets a 203 HTML sign-in page, not a 401."""
    import httpx

    fake_ado.raw_response = httpx.Response(
        203, text="<html>sign in</html>", headers={"content-type": "text/html"}
    )
    run = await execute(FakeHarness(), fake_ado)
    message = record(run, ErrorCode.ADO_FORBIDDEN)
    assert "non-JSON" in message


# ------------------------------------------------------------------- work_item_not_found


async def test_work_item_not_found_on_a_404(fake_ado: FakeAdo) -> None:
    run = await execute(FakeHarness(), fake_ado, workItem={"type": "Bug", "id": 9999})
    record(run, ErrorCode.WORK_ITEM_NOT_FOUND)


async def test_work_item_not_found_for_a_type_the_project_does_not_define(
    fake_ado: FakeAdo,
) -> None:
    """docs/contract.md §3: workItem.type is validated against the project's own types."""
    run = await execute(FakeHarness(), fake_ado, workItem={"type": "Nonsense", "id": 4123})
    message = record(run, ErrorCode.WORK_ITEM_NOT_FOUND)
    assert "Nonsense" in message


async def test_the_work_item_type_check_is_case_sensitive_like_ado(fake_ado: FakeAdo) -> None:
    run = await execute(FakeHarness(), fake_ado, workItem={"type": "bug", "id": 4123})
    record(run, ErrorCode.WORK_ITEM_NOT_FOUND)


# ------------------------------------------------------------------ repository_not_found


async def test_repository_not_found_on_a_404(fake_ado: FakeAdo) -> None:
    run = await execute(FakeHarness(), fake_ado, repository="no-such-repo")
    message = record(run, ErrorCode.REPOSITORY_NOT_FOUND)
    assert "no-such-repo" in message


async def test_repository_not_found_when_the_base_branch_does_not_exist(
    fake_ado: FakeAdo,
) -> None:
    run = await execute(FakeHarness(), fake_ado, baseBranch="no-such-base")
    message = record(run, ErrorCode.REPOSITORY_NOT_FOUND)
    assert "no-such-base" in message


# ------------------------------------------------------------------------- branch_exists


async def test_branch_exists_when_the_target_branch_is_already_on_the_remote(
    fake_ado: FakeAdo,
) -> None:
    fake_ado.extra_branches[f"refs/heads/{TARGET_BRANCH}"] = "a" * 40
    run = await execute(FakeHarness(), fake_ado)
    message = record(run, ErrorCode.BRANCH_EXISTS)
    assert TARGET_BRANCH in message


async def test_branch_exists_is_detected_before_anything_is_cloned(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    fake_ado.extra_branches[f"refs/heads/{TARGET_BRANCH}"] = "a" * 40
    run = await execute(FakeHarness(), fake_ado)
    assert run.error is not None and run.error.code is ErrorCode.BRANCH_EXISTS
    # The pre-existing branch is untouched on the remote.
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


async def test_reusing_the_base_branch_as_the_target_is_branch_exists(
    fake_ado: FakeAdo,
) -> None:
    run = await execute(FakeHarness(), fake_ado, branch=DEFAULT_BRANCH)
    record(run, ErrorCode.BRANCH_EXISTS)


# --------------------------------------------------------------------------- clone_failed


async def test_clone_failed_when_the_remote_is_unreachable(
    fake_ado: FakeAdo, tmp_path: Path
) -> None:
    """Azure DevOps reports the repository and its refs, but the clone itself fails."""
    fake_ado.remote_url_override = f"file://{tmp_path}/gone.git"
    run = await execute(FakeHarness(), fake_ado)
    record(run, ErrorCode.CLONE_FAILED)


async def test_clone_failed_does_not_leak_the_token_into_the_message(
    fake_ado: FakeAdo,
) -> None:
    fake_ado.remote_url_override = (
        "https://agent:test-ado-token-value@dev.azure.invalid/o/p/_git/r"
    )
    run = await execute(FakeHarness(), fake_ado)
    assert run.error is not None and run.error.code is ErrorCode.CLONE_FAILED
    assert "test-ado-token-value" not in run.error.message


# ------------------------------------------------------------------------- harness_failed


async def test_harness_failed_on_a_non_zero_exit(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(exit_code=7, summary="the model gave up"), fake_ado
    )
    message = record(run, ErrorCode.HARNESS_FAILED)
    assert "code 7" in message


async def test_harness_failed_when_max_files_changed_is_exceeded(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(files=6), fake_ado, settings=make_settings(max_files_changed=5)
    )
    message = record(run, ErrorCode.HARNESS_FAILED)
    assert "T2PR_MAX_FILES_CHANGED" in message


async def test_harness_failed_when_the_cli_cannot_be_started(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(raises=RunFailure(ErrorCode.HARNESS_FAILED, "could not start 'codex'")),
        fake_ado,
    )
    record(run, ErrorCode.HARNESS_FAILED)


# --------------------------------------------------------------------- harness_no_changes


async def test_harness_no_changes_when_the_working_tree_is_untouched(
    fake_ado: FakeAdo,
) -> None:
    run = await execute(ScriptedHarness(files=0), fake_ado)
    record(run, ErrorCode.HARNESS_NO_CHANGES)


# ---------------------------------------------------------------------------- push_failed


async def test_push_failed_when_the_remote_rejects_the_push(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    hook = bare_repo / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    run = await execute(FakeHarness(), fake_ado)
    record(run, ErrorCode.PUSH_FAILED)
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


# ------------------------------------------------------------------------------ pr_failed


async def test_pr_failed_when_azure_devops_rejects_the_pull_request(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    fake_ado.force["pull_request"] = 404
    run = await execute(FakeHarness(), fake_ado)
    record(run, ErrorCode.PR_FAILED)
    # And the branch pushed a moment earlier is gone again (docs/architecture.md §3).
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


@pytest.mark.parametrize("status", [400, 409, 500])
async def test_pr_failed_for_any_other_refusal_of_the_pull_request(
    fake_ado: FakeAdo, bare_repo: Path, status: int
) -> None:
    """docs/architecture.md §3 makes every failure of step 9 ``pr_failed``, not only the 404.

    Azure DevOps answers 409 when an active pull request already exists for the branch
    pair and 400 for an unusable source or target ref -- the two likeliest real failures.
    ``internal`` would tell the caller nothing was pushed, and by this point something was.
    """
    fake_ado.force["pull_request"] = status
    run = await execute(FakeHarness(), fake_ado)
    record(run, ErrorCode.PR_FAILED)
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


# -------------------------------------------------------------------------------- timeout


async def test_timeout_when_the_harness_exceeds_its_budget(fake_ado: FakeAdo) -> None:
    run = await execute(
        ScriptedHarness(
            raises=RunFailure(ErrorCode.TIMEOUT, "the coding harness exceeded its 60s budget")
        ),
        fake_ado,
    )
    message = record(run, ErrorCode.TIMEOUT)
    assert "budget" in message


async def test_timeout_is_produced_by_a_real_subprocess_overrun(fake_ado: FakeAdo) -> None:
    """Not just a raised sentinel: a real CLI that outruns its budget.

    ``T2PR_MAX_RUN_MINUTES`` has a one-minute floor, so the harness asserts it received
    the configured budget (proving the value propagates) and then uses a one-second
    budget for the actual overrun, to keep the suite fast.
    """
    import sys

    from ticket_to_pr.harness import clean_env, run_cli

    seen: list[int] = []

    class SlowHarness:
        name = "slow"

        def preflight(self, cfg: Any) -> None:
            return None

        def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> Any:
            seen.append(timeout_s)
            run_cli(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                repo_dir=repo_dir,
                prompt=prompt,
                timeout_s=1,
                env=clean_env(),
            )
            raise AssertionError("unreachable")

    run = await execute(SlowHarness(), fake_ado, settings=make_settings(max_run_minutes=1))
    assert seen == [60]
    record(run, ErrorCode.TIMEOUT)


async def test_timeout_leaves_no_branch_on_the_remote(
    fake_ado: FakeAdo, bare_repo: Path
) -> None:
    await execute(
        ScriptedHarness(raises=RunFailure(ErrorCode.TIMEOUT, "over budget")), fake_ado
    )
    assert f"refs/heads/{TARGET_BRANCH}" not in remote_branches(bare_repo)


# ------------------------------------------------------------------------------- internal


async def test_internal_for_an_unmapped_exception(fake_ado: FakeAdo) -> None:
    run = await execute(ScriptedHarness(raises=ValueError("a genuine bug")), fake_ado)
    message = record(run, ErrorCode.INTERNAL)
    # The traceback text may contain work-item content, so it must not be echoed.
    assert "a genuine bug" not in message
    assert message == "an unexpected internal error occurred"


async def test_internal_for_an_unexpected_azure_devops_status(fake_ado: FakeAdo) -> None:
    fake_ado.force["work_item_types"] = 500
    run = await execute(FakeHarness(), fake_ado)
    message = record(run, ErrorCode.INTERNAL)
    assert "500" in message


async def test_internal_for_a_transport_failure(fake_ado: FakeAdo) -> None:
    import httpx

    fake_ado.transport_error = httpx.ConnectError("no route to host")
    run = await execute(FakeHarness(), fake_ado)
    message = record(run, ErrorCode.INTERNAL)
    assert "no route to host" not in message  # exception text is never echoed verbatim


# --------------------------------------------------------------------------- exhaustiveness


def test_every_error_code_has_a_test_that_produces_it(
    request: pytest.FixtureRequest,
) -> None:
    """docs/contract.md §6 lists eleven codes. Each one above was actually observed.

    Must stay the last test in this module: pytest runs a module in definition order, so
    by the time it runs every producing test above has contributed to ``PRODUCED``.
    """
    selected = {
        item.name.partition("[")[0]
        for item in request.session.items
        if item.path == request.node.path
    }
    declared = {name for name in globals() if name.startswith("test_")}
    if not declared <= selected:
        pytest.skip("only meaningful when the whole module runs")
    missing = set(ErrorCode) - PRODUCED
    assert not missing, f"no test produces: {sorted(c.value for c in missing)}"
