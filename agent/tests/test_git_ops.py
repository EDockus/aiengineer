"""Git operations against a real ``file://`` remote (docs/architecture.md §3 and §4.2).

Nothing here is mocked: a bare repository is created on disk and the shipped
``git_ops`` functions clone from it, branch, commit and push into it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_to_pr import git_ops
from ticket_to_pr.models import ErrorCode, RunFailure

from .conftest import DEFAULT_BRANCH, git, remote_branches, seed_bare_repo

TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.a-real-looking-ado-access-token"


@pytest.fixture
def cloned(bare_repo: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "work" / "repo"
    git_ops.clone(
        remote_url=f"file://{bare_repo}", base_branch=DEFAULT_BRANCH, dest=dest, token=TOKEN
    )
    return dest


# ------------------------------------------------------------------------ clone and branch


def test_clone_is_shallow_and_single_branch(bare_repo: Path, tmp_path: Path) -> None:
    """docs/architecture.md §3 step 4: a shallow, single-branch clone."""
    # Give the remote more history than the requested depth, and a second branch.
    seed = bare_repo.parent / f"{bare_repo.stem}-seed"
    for n in range(3):
        (seed / f"c{n}.txt").write_text("x", encoding="utf-8")
        git(["add", "-A"], cwd=seed)
        git(["commit", "-m", f"commit {n}"], cwd=seed)
    git(["push", "origin", DEFAULT_BRANCH], cwd=seed)
    git(["push", "origin", f"{DEFAULT_BRANCH}:refs/heads/other"], cwd=seed)

    dest = tmp_path / "repo"
    git_ops.clone(
        remote_url=f"file://{bare_repo}",
        base_branch=DEFAULT_BRANCH,
        dest=dest,
        token=TOKEN,
        depth=1,
    )
    assert (dest / "README.md").exists()
    assert (dest / ".git" / "shallow").exists()
    assert git(["rev-list", "--count", "HEAD"], cwd=dest).strip() == "1"
    assert "other" not in git(["branch", "-r"], cwd=dest)


def test_clone_failure_maps_to_clone_failed(tmp_path: Path) -> None:
    with pytest.raises(RunFailure) as exc:
        git_ops.clone(
            remote_url=f"file://{tmp_path}/nope.git",
            base_branch="main",
            dest=tmp_path / "repo",
            token=TOKEN,
        )
    assert exc.value.code is ErrorCode.CLONE_FAILED


def test_clone_of_a_missing_base_branch_maps_to_clone_failed(
    bare_repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(RunFailure) as exc:
        git_ops.clone(
            remote_url=f"file://{bare_repo}",
            base_branch="no-such-branch",
            dest=tmp_path / "repo",
            token=TOKEN,
        )
    assert exc.value.code is ErrorCode.CLONE_FAILED


def test_create_branch_checks_it_out(cloned: Path) -> None:
    git_ops.create_branch(cloned, "bugfix/4123-null-ref")
    head = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cloned).strip()
    assert head == "bugfix/4123-null-ref"


# --------------------------------------------------------------------- ref-name defence


@pytest.mark.parametrize(
    "name",
    ["--upload-pack=touch /tmp/pwn", "-rf", "a b", "a;b", "a$(b)", "a`b`", "a|b", "a\nb",
     "a'b", 'a"b', "a&b", "a>b"],
)
def test_unsafe_ref_names_are_refused_before_reaching_git(cloned: Path, name: str) -> None:
    for call in (
        lambda: git_ops.create_branch(cloned, name),
        lambda: git_ops.push(cloned, branch=name, token=TOKEN),
        lambda: git_ops.commit_count(cloned, name),
    ):
        with pytest.raises(RunFailure) as exc:
            call()
        assert exc.value.code is ErrorCode.INTERNAL
        assert "unsafe ref name" in exc.value.message


def test_clone_refuses_an_unsafe_base_branch(tmp_path: Path) -> None:
    with pytest.raises(RunFailure, match="unsafe ref name"):
        git_ops.clone(
            remote_url="file:///tmp/x.git",
            base_branch="--upload-pack=touch /tmp/pwn",
            dest=tmp_path / "r",
            token=TOKEN,
        )


# ---------------------------------------------------------- the token never touches disk


def test_the_token_is_not_written_into_git_config(cloned: Path) -> None:
    """docs/architecture.md §4.2: the harness must not be able to read a credential off disk."""
    config = (cloned / ".git" / "config").read_text(encoding="utf-8")
    assert TOKEN not in config
    assert "extraheader" not in config.casefold()
    for path in cloned.rglob("*"):
        if path.is_file() and path.stat().st_size < 1_000_000:
            assert TOKEN not in path.read_bytes().decode("utf-8", "ignore")


def test_auth_env_uses_the_scheme_it_is_given() -> None:
    """A PAT is Basic, an Entra token is Bearer, and git will not tell you which is wrong.

    Sending ``bearer`` with a PAT does not produce a 401 from Azure DevOps -- git reports
    ``fatal: Authentication failed``, which reads like a permissions problem and is not
    one. This cost one failed deployment before the scheme was threaded through.
    """
    assert git_ops.auth_env(TOKEN)["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {TOKEN}"
    bearer = git_ops.auth_env(TOKEN, "bearer")
    assert bearer["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: bearer {TOKEN}"


def test_auth_env_passes_the_token_only_through_git_config_env() -> None:
    env = git_ops.auth_env(TOKEN)
    # Basic is the default, because Azure DevOps takes a PAT that way (deploy.md §3).
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {TOKEN}"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Nothing inherited that was not asked for.
    assert set(env) == {
        "PATH", "HOME", "LANG", "LC_ALL", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS",
        "GIT_CONFIG_NOSYSTEM", "GIT_ADVICE", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1",
    }


def test_git_errors_are_redacted_before_they_reach_a_caller(tmp_path: Path) -> None:
    with pytest.raises(RunFailure) as exc:
        git_ops.clone(
            remote_url=f"https://user:{TOKEN}@dev.azure.invalid/x/_git/y",
            base_branch="main",
            dest=tmp_path / "r",
            token=TOKEN,
        )
    assert TOKEN not in exc.value.message
    assert "eyJ" not in exc.value.message


def test_redact_covers_bearer_headers_urls_and_jwts() -> None:
    assert "<redacted>" in git_ops._redact("Authorization: bearer abc123")
    assert "<redacted>@" in git_ops._redact("https://user:pw@host/x")
    assert git_ops._redact(f"failed with {TOKEN}").endswith("<redacted>")


# ------------------------------------------------------------ credential-helper stripping


def test_strip_credential_config_removes_every_credential_key(cloned: Path) -> None:
    config = cloned / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[credential]\n\thelper = store\n\tusername = agent\n"
        + '[http "https://dev.azure.com"]\n\textraheader = AUTHORIZATION: bearer leak\n',
        encoding="utf-8",
    )
    git_ops.strip_credential_config(cloned)
    after = config.read_text(encoding="utf-8").casefold()
    assert "helper" not in after
    assert "extraheader" not in after
    assert "leak" not in after
    assert "username" not in after


def test_strip_credential_config_leaves_ordinary_settings_alone(cloned: Path) -> None:
    git_ops.strip_credential_config(cloned)
    after = (cloned / ".git" / "config").read_text(encoding="utf-8")
    assert "[remote " in after
    assert "url" in after


def test_strip_credential_config_is_safe_when_there_is_no_config(tmp_path: Path) -> None:
    git_ops.strip_credential_config(tmp_path / "not-a-repo")  # must not raise


def test_strip_credential_config_handles_hyphenated_spellings(cloned: Path) -> None:
    config = cloned / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[http]\n\textra-header = bearer leak\n',
        encoding="utf-8",
    )
    git_ops.strip_credential_config(cloned)
    assert "leak" not in config.read_text(encoding="utf-8")


# ------------------------------------------------------------------ hooks the harness plants
#
# The run directory is shared with the harness uid by design -- it has to be, the harness
# writes the code there -- so the harness can equally write ``.git/hooks/`` and
# ``.git/config``. Every git command the agent runs *after* the harness then executes
# whatever it left behind, as the server uid; ``push`` executes it holding the PAT.


def _tripwire(marker: Path) -> str:
    """A hook that records the environment it was handed, and that it ran at all."""
    return f'#!/bin/sh\nenv > "{marker}"\nexit 0\n'


def _plant(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_tripwire(marker), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def ready_to_push(cloned: Path) -> Path:
    git_ops.create_branch(cloned, "feature/x")
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    return cloned


def test_push_does_not_run_a_planted_pre_push_hook(
    ready_to_push: Path, bare_repo: Path, tmp_path: Path
) -> None:
    """The finding: ``push`` runs with ``auth_env``, so a ``pre-push`` hook reads the PAT.

    ``commit_all`` has carried ``--no-verify`` since it was written and ``push`` did not,
    which is the whole of the difference. Before the fix this hook ran, and its environment
    held ``GIT_CONFIG_VALUE_0=AUTHORIZATION: basic <token>``.
    """
    marker = tmp_path / "pre-push.env"
    git_ops.commit_all(ready_to_push, message="m", author_name="A", author_email="a@b.test")
    _plant(ready_to_push / ".git" / "hooks" / "pre-push", marker)

    git_ops.push(ready_to_push, branch="feature/x", token=TOKEN)

    assert not marker.exists(), "a planted pre-push hook executed while git held the PAT"
    assert "refs/heads/feature/x" in remote_branches(bare_repo)


def test_push_ignores_a_core_hookspath_redirect(
    ready_to_push: Path, bare_repo: Path, tmp_path: Path
) -> None:
    """The half that emptying ``.git/hooks/`` cannot reach.

    ``core.hooksPath`` sends git somewhere else entirely, so a scrubbed ``.git/hooks/``
    is simply never consulted. Only forcing the key on the command line -- where nothing
    on disk outranks it -- closes this.
    """
    marker = tmp_path / "redirected.env"
    elsewhere = tmp_path / "elsewhere"
    _plant(elsewhere / "pre-push", marker)
    git(["config", "core.hooksPath", str(elsewhere)], cwd=ready_to_push)
    git_ops.commit_all(ready_to_push, message="m", author_name="A", author_email="a@b.test")

    git_ops.push(ready_to_push, branch="feature/x", token=TOKEN)

    assert not marker.exists(), "core.hooksPath redirected git to an attacker-owned hook"
    assert "refs/heads/feature/x" in remote_branches(bare_repo)


def test_no_post_harness_git_command_runs_a_planted_hook(
    ready_to_push: Path, tmp_path: Path
) -> None:
    """``--no-verify`` alone is not enough, and neither is looking only at ``push``.

    ``post-commit``, ``prepare-commit-msg``, ``reference-transaction`` and
    ``post-index-change`` all run regardless of ``--no-verify`` -- they are not among the
    hooks git offers to skip -- so ``commit_all`` and ``changed_files`` were exposed too.
    """
    names = (
        "pre-push",
        "pre-commit",
        "commit-msg",
        "prepare-commit-msg",
        "post-commit",
        "reference-transaction",
        "post-index-change",
    )
    for name in names:
        _plant(ready_to_push / ".git" / "hooks" / name, tmp_path / f"{name}.env")

    git_ops.changed_files(ready_to_push)
    git_ops.commit_all(ready_to_push, message="m", author_name="A", author_email="a@b.test")
    git_ops.head_object_id(ready_to_push)
    git_ops.commit_count(ready_to_push, DEFAULT_BRANCH)
    git_ops.push(ready_to_push, branch="feature/x", token=TOKEN)

    ran = sorted(name for name in names if (tmp_path / f"{name}.env").exists())
    assert ran == [], f"post-harness git executed planted hooks: {ran}"


def test_strip_credential_config_empties_the_hooks_directory(cloned: Path) -> None:
    hooks = cloned / ".git" / "hooks"
    _plant(hooks / "pre-push", cloned.parent / "unused")
    (hooks / "subdir").mkdir(exist_ok=True)
    (hooks / "subdir" / "nested").write_text("x", encoding="utf-8")

    git_ops.strip_credential_config(cloned)

    assert list(hooks.iterdir()) == []


@pytest.mark.parametrize(
    ("section", "option", "value"),
    [
        ("core", "hooksPath", "/tmp/evil-hooks"),
        ("core", "fsmonitor", "/tmp/evil.sh"),
        ("core", "sshCommand", "/tmp/evil.sh"),
        ("core", "pager", "/tmp/evil.sh"),
        ("core", "askpass", "/tmp/evil.sh"),
        ('filter "evil"', "clean", "/tmp/evil.sh"),
        ('filter "evil"', "smudge", "/tmp/evil.sh"),
        ('diff "evil"', "command", "/tmp/evil.sh"),
        ('diff "evil"', "textconv", "/tmp/evil.sh"),
        ("include", "path", "/tmp/evil-config"),
        ("uploadpack", "packObjectsHook", "/tmp/evil.sh"),
    ],
)
def test_strip_credential_config_removes_keys_git_would_execute(
    cloned: Path, section: str, option: str, value: str
) -> None:
    """Not credentials, but every one of these is a command git runs as the server uid --
    which is the uid holding the PAT, so each is credential-equivalent."""
    config = cloned / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + f"\n[{section}]\n\t{option} = {value}\n",
        encoding="utf-8",
    )
    git_ops.strip_credential_config(cloned)
    after = config.read_text(encoding="utf-8")
    assert value not in after
    assert option.casefold() not in after.casefold()


def test_every_git_call_forces_hooks_path_on_the_command_line(cloned: Path) -> None:
    """Stripping ``.git/config`` cannot cover the window between the strip and the command.
    The command line can, because nothing on disk outranks it."""
    args = git_ops.hardening_args()
    assert args[0] == "-c"
    assert args[1] == f"core.hooksPath={git_ops.empty_hooks_dir()}"
    assert "-c" in args and "core.fsmonitor=false" in args
    hooks_dir = git_ops.empty_hooks_dir()
    assert hooks_dir.is_dir()
    assert list(hooks_dir.iterdir()) == []
    # Outside the run directory, so the harness uid cannot create a hook inside it.
    assert cloned.parent not in hooks_dir.parents


# ------------------------------------------------------------------ status, commit, push


def test_changed_files_reports_new_modified_and_untracked(cloned: Path) -> None:
    assert git_ops.changed_files(cloned) == []
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    (cloned / "README.md").write_text("changed", encoding="utf-8")
    (cloned / "pkg").mkdir()
    (cloned / "pkg" / "deep.txt").write_text("y", encoding="utf-8")
    changed = set(git_ops.changed_files(cloned))
    assert changed == {"new.txt", "README.md", "pkg/deep.txt"}


def test_commit_all_uses_the_configured_author(cloned: Path) -> None:
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(
        cloned, message="Bug #1: t\n\nbody\n", author_name="Agent", author_email="a@b.test"
    )
    out = git(["log", "-1", "--format=%an|%ae|%s|%b"], cwd=cloned).strip()
    assert out.startswith("Agent|a@b.test|Bug #1: t|body")


def test_commit_message_with_shell_metacharacters_is_safe(cloned: Path) -> None:
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(
        cloned,
        message="Bug #1: `touch /tmp/pwn`; $(id) && rm -rf /\n",
        author_name="A",
        author_email="a@b.test",
    )
    assert "$(id)" in git(["log", "-1", "--format=%s"], cwd=cloned)


def test_commit_with_nothing_staged_maps_to_push_failed(cloned: Path) -> None:
    with pytest.raises(RunFailure) as exc:
        git_ops.commit_all(cloned, message="m", author_name="A", author_email="a@b.test")
    assert exc.value.code is ErrorCode.PUSH_FAILED


def test_push_puts_the_branch_on_the_remote(cloned: Path, bare_repo: Path) -> None:
    git_ops.create_branch(cloned, "feature/x")
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(cloned, message="m", author_name="A", author_email="a@b.test")
    git_ops.push(cloned, branch="feature/x", token=TOKEN)
    assert "refs/heads/feature/x" in remote_branches(bare_repo)


def test_push_failure_maps_to_push_failed(cloned: Path, bare_repo: Path) -> None:
    hook = bare_repo / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git_ops.create_branch(cloned, "feature/x")
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(cloned, message="m", author_name="A", author_email="a@b.test")
    with pytest.raises(RunFailure) as exc:
        git_ops.push(cloned, branch="feature/x", token=TOKEN)
    assert exc.value.code is ErrorCode.PUSH_FAILED


def test_head_object_id_and_commit_count(cloned: Path) -> None:
    before = git_ops.head_object_id(cloned)
    assert len(before) == 40
    assert git_ops.commit_count(cloned, DEFAULT_BRANCH) == 0
    git_ops.create_branch(cloned, "feature/x")
    (cloned / "new.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(cloned, message="m", author_name="A", author_email="a@b.test")
    assert git_ops.head_object_id(cloned) != before
    assert git_ops.commit_count(cloned, DEFAULT_BRANCH) == 1


def test_commit_count_falls_back_to_one_when_the_base_ref_is_gone(cloned: Path) -> None:
    git(["update-ref", "-d", f"refs/remotes/origin/{DEFAULT_BRANCH}"], cwd=cloned)
    assert git_ops.commit_count(cloned, DEFAULT_BRANCH) == 1


def test_git_timeout_maps_to_the_timeout_code(cloned: Path) -> None:
    with pytest.raises(RunFailure) as exc:
        git_ops._git(
            ["-c", "alias.slow=!sleep 5", "slow"],
            cwd=cloned,
            timeout=1,
            what="slow git command",
        )
    assert exc.value.code is ErrorCode.TIMEOUT


def test_seeded_remote_starts_with_only_the_default_branch(tmp_path: Path) -> None:
    bare = seed_bare_repo(tmp_path / "b.git")
    assert list(remote_branches(bare)) == [f"refs/heads/{DEFAULT_BRANCH}"]
