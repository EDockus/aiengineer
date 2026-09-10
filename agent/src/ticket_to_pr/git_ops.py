"""Git operations: clone, branch, inspect, commit, push, delete.

Two rules shape this module (SECURITY.md "Credentials" and docs/architecture.md §4.2):

* The Azure DevOps credential is passed through ``GIT_CONFIG_*`` environment
  variables, never through argv (where any process could read it from ``ps``) and never
  written into ``.git/config`` (where the harness could read it off disk).
* :func:`strip_credential_config` runs both *before* and *after* the harness, so neither
  a repo that arrived with a credential helper baked in nor one the harness rewrote
  mid-run can leak one -- and every git invocation is additionally hardened on the
  command line by :func:`hardening_args`, where nothing on disk can override it.

Every function here is blocking; ``run.py`` calls them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import atexit
import configparser
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

from .models import ErrorCode, RunFailure
from .redact import redact as _redact

log = logging.getLogger(__name__)

#: Resolved once so every call uses an absolute path to a known binary.
GIT = shutil.which("git") or "git"

SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: Keys that could make git hand credentials to a subprocess or a remote.
CREDENTIAL_KEYS = ("extraheader", "helper", "username", "password", "proxyauthmethod")

#: Keys whose *value is a command git executes*. A harness that can write into the run
#: directory can write ``.git/config`` too, and each of these turns a later ``git status``,
#: ``git add``, ``git commit`` or ``git push`` into arbitrary code execution as the server
#: uid -- which is the uid holding ``T2PR_ADO_PAT``, so every one of them is
#: credential-equivalent even when the credential is not in that command's environment.
#: ``push`` is the sharpest of them: it runs with :func:`auth_env`, so a ``pre-push`` hook
#: reads the PAT straight out of ``GIT_CONFIG_VALUE_0``.
#:
#: Matched by section+option, so ``filter.evil.clean``, ``diff.evil.command`` and
#: ``diff.evil.textconv`` are covered along with the fixed ``core.*`` names.
COMMAND_KEYS = (
    ("core", "hookspath"),
    ("core", "fsmonitor"),
    ("core", "sshcommand"),
    ("core", "alternaterefscommand"),
    ("core", "pager"),
    ("core", "editor"),
    ("core", "askpass"),
    ("filter", None),
    ("diff", "command"),
    (None, "textconv"),
    ("uploadpack", "packobjectshook"),
    ("sequence", "editor"),
    # An included file is config too, and configparser does not follow includes -- so the
    # only safe thing to do with one is refuse to let git follow it either.
    ("include", None),
    ("includeif", None),
)

_hooks_lock = threading.Lock()
_hooks_dir: Path | None = None


def empty_hooks_dir() -> Path:
    """An empty directory to point ``core.hooksPath`` at, outside anything a harness owns.

    Emptying ``.git/hooks/`` is not enough on its own: a ``core.hooksPath`` in
    ``.git/config`` redirects git to a directory the harness *does* control, and the empty
    one is never consulted. So the real defence is forcing ``core.hooksPath`` on the
    command line, where no file on disk can override it, at a path the harness cannot write.

    Created 0500 under the server's ``TMPDIR`` -- outside the 2770 shared run directory --
    so the harness uid can neither traverse into it nor create a hook inside it. One per
    process, removed at exit.
    """
    global _hooks_dir
    with _hooks_lock:
        if _hooks_dir is None or not _hooks_dir.is_dir():
            path = Path(tempfile.mkdtemp(prefix="t2pr-nohooks-"))
            path.chmod(0o500)
            atexit.register(shutil.rmtree, path, ignore_errors=True)
            _hooks_dir = path
        return _hooks_dir


def hardening_args() -> list[str]:
    """``-c`` overrides applied to *every* git invocation this module makes.

    Command-line ``-c`` outranks ``.git/config``, so these hold even against a repository
    the harness has just rewritten -- and they hold for the window between
    :func:`strip_credential_config` and the command, which stripping alone cannot cover.
    Belt and braces with the strip, deliberately: neither is sufficient by itself.
    """
    return [
        "-c",
        f"core.hooksPath={empty_hooks_dir()}",
        "-c",
        "core.fsmonitor=false",
    ]


def _base_env() -> dict[str, str]:
    """A deliberately small environment for git. Nothing inherited that we did not ask for."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - only a fallback for HOME
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # Never let git block on an interactive prompt inside a container.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ADVICE": "0",
    }
    return env


def auth_env(token: str, scheme: str = "basic") -> dict[str, str]:
    """Environment carrying the Azure DevOps credential as an HTTP extra header.

    ``scheme`` must match what produced ``token``: ``basic`` for a Personal Access Token
    (the default -- see docs/deploy.md §3), ``bearer`` for an Entra token. Getting this
    wrong does not fail loudly at the header, it fails later as
    ``fatal: Authentication failed`` from git, which reads as a permissions problem and is
    not one.

    ``GIT_CONFIG_COUNT`` config is applied in-memory for this process only: it does not
    appear in argv and is not persisted to ``.git/config``.
    """
    env = _base_env()
    env.update(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: {scheme} {token}",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
        }
    )
    return env


def _git(
    args: Sequence[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    code: ErrorCode = ErrorCode.INTERNAL,
    what: str = "git command",
) -> str:
    """Run git and return stdout, mapping failure to a contract error code."""
    argv = [GIT, *hardening_args(), *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, validated refs
            argv,
            cwd=str(cwd) if cwd else None,
            env=env or _base_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunFailure(ErrorCode.TIMEOUT, f"{what} timed out after {timeout}s") from exc
    except OSError as exc:
        raise RunFailure(code, f"{what} could not be started: {exc.strerror}") from exc

    if proc.returncode != 0:
        raise RunFailure(code, f"{what} failed: {_redact(proc.stderr).strip()[:800]}")
    return proc.stdout


def _check_ref(name: str) -> str:
    if not SAFE_REF_RE.match(name) or name.startswith("-"):
        raise RunFailure(ErrorCode.INTERNAL, f"refusing to use unsafe ref name {name!r}")
    return name


def clone(
    *,
    remote_url: str,
    base_branch: str,
    dest: Path,
    token: str,
    scheme: str = "basic",
    depth: int = 50,
) -> None:
    """Shallow-clone ``base_branch`` into ``dest`` (docs/architecture.md §3 step 4)."""
    _check_ref(base_branch)
    _git(
        [
            "clone",
            "--depth",
            str(depth),
            "--single-branch",
            "--branch",
            base_branch,
            "--",
            remote_url,
            str(dest),
        ],
        cwd=None,
        env=auth_env(token, scheme),
        code=ErrorCode.CLONE_FAILED,
        what=f"clone of {_redact(remote_url)} branch {base_branch!r}",
    )
    configure_identity(dest)


def configure_identity(repo_dir: Path) -> None:
    """Set a repo-local identity so a harness that commits anyway does not fail on it."""
    _git(["config", "user.name", "Ticket-to-PR agent"], cwd=repo_dir)
    _git(["config", "user.email", "ticket-to-pr@noreply.local"], cwd=repo_dir)


def create_branch(repo_dir: Path, branch: str) -> None:
    _check_ref(branch)
    _git(
        ["checkout", "-b", branch],
        cwd=repo_dir,
        code=ErrorCode.CLONE_FAILED,
        what=f"creating branch {branch!r}",
    )


def _is_dangerous(section: str, option: str) -> bool:
    """Is this ``.git/config`` entry a credential, or a command git would execute?"""
    # ``[filter "evil"]`` parses as the section ``filter "evil"``; the sub-section name is
    # attacker-chosen, so match on the leading word only.
    head = section.casefold().replace("-", "").split(None, 1)[0] if section.strip() else ""
    lowered = option.replace("-", "").casefold()
    if head.startswith("credential") or any(key in lowered for key in CREDENTIAL_KEYS):
        return True
    return any(
        (want_section is None or head == want_section)
        and (want_option is None or lowered == want_option)
        for want_section, want_option in COMMAND_KEYS
    )


def strip_credential_config(repo_dir: Path) -> None:
    """Disarm ``.git/config`` and ``.git/hooks/`` before git is run against this repo.

    Called immediately before the harness runs *and again as soon as it returns*. Running
    it only beforehand was the gap a security review opened this module on: the harness
    shares the run directory by design (it has to write the code), so it can equally write
    ``.git/hooks/pre-push`` and ``.git/config``, and every git command after it -- status,
    add, commit, rev-parse, push -- then executed whatever it left there. ``push`` executes
    it holding the Azure DevOps PAT.

    Two classes go: the credential keys (:data:`CREDENTIAL_KEYS`, the original job) and
    every key whose value git runs as a command (:data:`COMMAND_KEYS`). Hooks are emptied
    rather than deleted, so the directory keeps its permissions.

    This is the on-disk half. The other half is :func:`hardening_args`, which forces
    ``core.hooksPath`` on the command line -- necessary because a ``core.hooksPath`` here
    points git somewhere else entirely, where emptying ``.git/hooks/`` reaches nothing.
    """
    git_dir = repo_dir / ".git"
    _empty_hooks(git_dir)
    config_path = git_dir / "config"
    if not config_path.exists():
        return
    parser = configparser.RawConfigParser(strict=False)
    parser.read(config_path, encoding="utf-8")
    changed = False
    for section in parser.sections():
        for option in list(parser[section]):
            if _is_dangerous(section, option):
                parser.remove_option(section, option)
                changed = True
        if section.casefold().startswith("credential") and not parser[section]:
            parser.remove_section(section)
            changed = True
    if changed:
        with config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        log.info("stripped credential and command configuration from %s", config_path)


def _empty_hooks(git_dir: Path) -> None:
    """Remove everything from ``.git/hooks/``.

    ``--no-verify`` covers the hooks git offers to skip; it does not cover ``post-commit``,
    ``prepare-commit-msg``, ``reference-transaction`` or ``post-index-change``, which run
    regardless. Best-effort: a hook that cannot be removed is logged, and
    :func:`hardening_args` still points git away from the whole directory.
    """
    hooks = git_dir / "hooks"
    if not hooks.is_dir():
        return
    for entry in hooks.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:  # pragma: no cover - only a permissions oddity
            log.warning("could not remove the git hook %s: %s", entry, exc)
        else:
            log.info("removed the git hook %s", entry.name)


def changed_files(repo_dir: Path) -> list[str]:
    """Paths with any working-tree change, including untracked files."""
    out = _git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_dir,
        what="git status",
    )
    paths: list[str] = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            paths.append(entry[3:])
    return paths


def commit_all(repo_dir: Path, *, message: str, author_name: str, author_email: str) -> None:
    """Stage everything and make one commit authored by the agent identity."""
    _git(["add", "--all", "--", "."], cwd=repo_dir, what="git add")
    argv = [
        *hardening_args(),
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "--no-verify",
        "--file=-",
    ]
    env = _base_env()
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [GIT, *argv],
            cwd=str(repo_dir),
            env=env,
            input=message,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunFailure(ErrorCode.PUSH_FAILED, "git commit timed out") from exc
    if proc.returncode != 0:
        raise RunFailure(
            ErrorCode.PUSH_FAILED, f"git commit failed: {_redact(proc.stderr).strip()[:800]}"
        )


def push(repo_dir: Path, *, branch: str, token: str, scheme: str = "basic") -> None:
    """Push the run's branch. The one git command that carries the credential.

    ``--no-verify`` skips ``pre-push``, which is the hook a harness would plant precisely
    because this is where the PAT is: it runs with :func:`auth_env`, so the hook would read
    ``AUTHORIZATION: basic <PAT>`` out of ``GIT_CONFIG_VALUE_0``. ``commit_all`` has carried
    the same flag since it was written; this call did not.
    """
    _check_ref(branch)
    _git(
        [
            "push",
            "--no-verify",
            "--set-upstream",
            "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
        ],
        cwd=repo_dir,
        env=auth_env(token, scheme),
        code=ErrorCode.PUSH_FAILED,
        what=f"pushing branch {branch!r}",
    )


def head_object_id(repo_dir: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=repo_dir, what="git rev-parse").strip()


def commit_count(repo_dir: Path, base_branch: str) -> int:
    """Number of commits on HEAD that are not on the fetched base branch."""
    _check_ref(base_branch)
    try:
        out = _git(
            ["rev-list", "--count", f"origin/{base_branch}..HEAD"],
            cwd=repo_dir,
            what="git rev-list",
        )
    except RunFailure:
        return 1
    try:
        return int(out.strip())
    except ValueError:
        return 1
