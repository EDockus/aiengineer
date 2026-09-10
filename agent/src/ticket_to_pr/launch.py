"""Entrypoint launcher that scrubs credentials out of the exec-time environment of every
process it starts (security review HIGH-1, second round trip; CRITICAL, fifth).

Why this exists
---------------
:func:`ticket_to_pr.harness.clean_env` keeps credentials out of a harness's *own*
environment, but that does not put them out of the harness's *reach*. ``/proc/<pid>/environ``
reflects a process's **exec-time** environment, unaffected by anything that process does to
``os.environ`` afterwards, and the kernel lets any process read it for a pid at its own uid.
So every variable the container runtime injects is reachable by anything sharing a uid with
the process that received it. Two secrets travel that way:

* the **managed-identity plumbing** -- ``IDENTITY_ENDPOINT``/``IDENTITY_HEADER`` (or
  ``MSI_ENDPOINT``/``MSI_SECRET``) on a Container Apps / App Service / IMDS deployment. A
  four-line stdlib payload can read those out of ``/proc/1/environ`` and ask the identity
  endpoint for an Azure DevOps token; and
* the **Azure DevOps PAT** (``T2PR_ADO_PAT``, docs/deploy.md §3). Long-lived, org-wide
  ``vso.work vso.code_write``, valid until somebody revokes it -- strictly worse to leak
  than a token with an expiry.

The fix
-------
This module is the container ENTRYPOINT. It runs as pid 1, reads every credential-shaped
variable out of *its* environment, and then ``execve``\\s the real server -- and, before
that, the unprivileged runner -- with those variables **removed from the environment they
are exec'd with**. Because ``/proc/<pid>/environ`` is the exec-time environment, ``/proc/1/environ``
comes back clean for the life of the server, and so does the runner's.

The runner is the hop that matters most, and the one that was missed: it has already
dropped to the harness's *own* uid, so ``/proc/<runner>/environ`` is readable by every
coding CLI it spawns and no uid boundary stands between them. Nothing but
:data:`CREDENTIAL_NAME_FRAGMENTS` protects that read.

The values still have to reach the credential chain and ``Settings.from_env``, so they are
handed to the server on an anonymous pipe (an inherited fd, named in ``T2PR_CRED_FD``). The
server drains and closes that fd at the very first line of startup, before it binds a
socket, reads its configuration or runs any harness, and copies the values into its own
``os.environ`` -- which ``DefaultAzureCredential`` reads at ``get_token`` time and
``Settings.from_env`` reads the PAT from, and which (being a post-exec mutation) never
appears in ``/proc/1/environ``. The runner is handed nothing: it holds no credential of its
own and never needed one.

Residual, stated plainly
------------------------
This closes the *endpoint* shapes (Container Apps, App Service, IMDS): the secret is the
header/secret that lives only in the environment, and that is what is scrubbed. It does
**not** close a **workload-identity** deployment, where ``AZURE_FEDERATED_TOKEN_FILE``
points at a projected token file that stays on disk, readable by the harness uid at its
well-known path regardless of the environment variable. That residual is documented in
``SECURITY.md`` (Credentials); the durable defence for it is uid separation, noted there.
"""

from __future__ import annotations

import os
import socket
import sys

from .runner import RUNNER_FD_ENV

#: Name of the inherited-fd number the server drains its credentials from. Not a secret --
#: it is a small integer -- so it is fine for this to sit in the scrubbed environment.
CRED_FD_ENV = "T2PR_CRED_FD"

#: Substrings that mark an environment variable as identity plumbing or a raw credential:
#: a step on some path to a token, or a token itself. Every variable whose name contains
#: one of these is moved off the exec-time environment and delivered on the fd instead, so
#: the credential chain keeps working while ``/proc/<pid>/environ`` carries none of it.
#:
#: Identical to ``harness.FORBIDDEN_ENV_FRAGMENTS``, and ``test_launch.py`` asserts that
#: rather than leaving it to a comment. The two drifted apart once and the Azure DevOps PAT
#: fell through the gap: ``T2PR_ADO_PAT`` matched nothing here, so ``scrubbed_environ`` left
#: it in ``child_env``, ``_spawn_runner`` copied it into ``runner_env``, and the runner --
#: already dropped to the harness's own uid -- carried it in its exec-time environ where
#: ``/proc/<runner>/environ`` is readable by every CLI it spawns. Same-uid ``/proc`` reads
#: are permitted, so the launcher's scrub is the *only* defence on that hop.
#:
#: ``ADO_PAT``, not ``PAT``: matching is a substring test on the upper-cased name, and
#: ``PATH`` contains ``PAT``. Stripping ``PATH`` would break every spawn in the image.
CREDENTIAL_NAME_FRAGMENTS = (
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


#: uid/gid the harness runner drops to, and the shared group both it and the server sit in
#: so a cloned repository is writable by the harness and readable back by the server. These
#: match the accounts created in ``agent/Dockerfile``; overridable only for testing.
HARNESS_UID = int(os.environ.get("T2PR_HARNESS_UID", "10002"))
HARNESS_GID = int(os.environ.get("T2PR_HARNESS_GID", "10002"))
SERVER_UID = int(os.environ.get("T2PR_SERVER_UID", "10001"))
SERVER_GID = int(os.environ.get("T2PR_SERVER_GID", "10001"))
SHARED_GID = int(os.environ.get("T2PR_SHARED_GID", "10003"))


def _drop_privileges(uid: int, gid: int, *, extra_groups: tuple[int, ...] = ()) -> None:
    """Become ``uid``:``gid`` irreversibly.

    ``setgroups`` first, then ``setgid``, then ``setuid`` -- in that order, because after
    ``setuid`` the process no longer has the privilege to change its groups, and a
    supplementary group left behind would silently widen what it can read.
    """
    os.setgroups(list(extra_groups))
    os.setgid(gid)
    os.setuid(uid)


def _is_credential_var(name: str) -> bool:
    upper = name.upper()
    if upper == CRED_FD_ENV:  # our own handoff fd, delivered in the clear on purpose
        return False
    return any(fragment in upper for fragment in CREDENTIAL_NAME_FRAGMENTS)


def collect_credentials(environ: dict[str, str]) -> dict[str, str]:
    """The identity/credential variables to move off the exec-time environment."""
    return {name: value for name, value in environ.items() if _is_credential_var(name)}


def scrubbed_environ(environ: dict[str, str]) -> dict[str, str]:
    """A copy of ``environ`` with every credential-shaped variable removed."""
    return {name: value for name, value in environ.items() if not _is_credential_var(name)}


def _encode(creds: dict[str, str]) -> bytes:
    # NUL-separated KEY=VALUE records: values may contain anything except NUL, which no
    # environment value can hold, so this framing is unambiguous.
    return b"\0".join(f"{k}={v}".encode() for k, v in creds.items())


def absorb_credentials(environ: dict[str, str] | None = None) -> None:
    """Server side: read the credentials off ``T2PR_CRED_FD`` and into ``os.environ``.

    A no-op when the variable is absent (running outside the launcher, e.g. under the
    tests or the fake harness in dev mode). Runs at the very top of :func:`main`, before
    any socket is bound and long before any harness is spawned, then closes the fd so no
    later process can read the pipe out of ``/proc/1/fd``.
    """
    env = os.environ if environ is None else environ
    raw = env.pop(CRED_FD_ENV, None)
    if raw is None:
        return
    fd = int(raw)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    blob = b"".join(chunks)
    if not blob:
        return
    for record in blob.split(b"\0"):
        name, _, value = record.partition(b"=")
        if name:
            env[name.decode()] = value.decode()


def _exec_server(environ: dict[str, str]) -> None:
    """Replace this process with the real server under the given environment."""
    os.execve(  # noqa: S606 - fixed argv, this interpreter, no shell
        sys.executable, [sys.executable, "-m", "ticket_to_pr"], environ
    )


def _spawn_runner(child_env: dict[str, str]) -> socket.socket | None:
    """Fork the unprivileged harness runner; return the server's end of the socketpair.

    ``None`` when this process is not privileged enough to drop -- running the container
    as a plain user is a supported local-development shape, and there the server executes
    harnesses in-process exactly as it did before. In a deployed image the launcher starts
    as root and this always succeeds.
    """
    if os.geteuid() != 0:
        return None

    server_end, runner_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pid = os.fork()
    if pid == 0:  # child -- becomes the runner
        try:
            server_end.close()
            # No supplementary groups beyond the shared one: the harness uid must not
            # inherit anything that would let it read the server's files.
            _drop_privileges(HARNESS_UID, HARNESS_GID, extra_groups=(SHARED_GID,))
            fd = runner_end.fileno()
            os.set_inheritable(fd, True)
            runner_env = dict(child_env)
            runner_env[RUNNER_FD_ENV] = str(fd)
            runner_env.pop(CRED_FD_ENV, None)  # the runner never touches credentials
            # execve, not just fork: a forked child keeps its parent's *exec-time* environ,
            # so /proc/<runner>/environ would still carry whatever pid 1 was started with --
            # readable by a harness that shares this very uid. The re-exec is what cleans
            # it, and `child_env` is already scrubbed, so what lands here is the allowlist's
            # residue and nothing credential-shaped. Both halves are load-bearing: the PAT
            # leaked precisely because it was not in CREDENTIAL_NAME_FRAGMENTS and so
            # survived the scrub into this environment.
            os.execve(  # noqa: S606 - fixed argv, this interpreter, no shell
                sys.executable, [sys.executable, "-m", "ticket_to_pr.runner"], runner_env
            )
        except BaseException:  # pragma: no cover - a failed fork child must never return
            os._exit(70)
    runner_end.close()
    return server_end


def main() -> int:
    """Container ENTRYPOINT.

    Runs as root so it can put the harness on its own uid, then gives that privilege up:
    the runner is dropped to :data:`HARNESS_UID` and the server to :data:`SERVER_UID`, so
    by the time either executes anything, nothing in the container is root. The non-root
    requirement in docs/architecture.md §4.4 is about the *server*, and it still holds.
    """
    source = dict(os.environ)
    creds = collect_credentials(source)
    child_env = scrubbed_environ(source)

    server_end = _spawn_runner(child_env)
    if server_end is not None:
        fd = server_end.fileno()
        os.set_inheritable(fd, True)
        child_env[RUNNER_FD_ENV] = str(fd)

    if creds:
        read_fd, write_fd = os.pipe()
        # The payload is a handful of small variables, far under the pipe buffer, so this
        # write cannot block; close the write end so the reader sees EOF.
        os.write(write_fd, _encode(creds))
        os.close(write_fd)
        # Python marks new fds close-on-exec (PEP 446); the server must inherit this one.
        os.set_inheritable(read_fd, True)
        child_env[CRED_FD_ENV] = str(read_fd)

    if os.geteuid() == 0:
        # Last privileged act. The shared group is what lets the server read back and
        # delete what the harness wrote into a run directory.
        _drop_privileges(SERVER_UID, SERVER_GID, extra_groups=(SHARED_GID,))

    _exec_server(child_env)
    return 0  # unreachable: execve does not return on success


if __name__ == "__main__":
    raise SystemExit(main())
