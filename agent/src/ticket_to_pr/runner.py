"""Unprivileged harness runner: the process that actually executes a coding CLI.

Why this exists
---------------
Three security findings in a row reached the same credential by three different routes --
the environment allowlist, ``/proc/<server>/environ``, and a writable shared ``$HOME`` --
because the agent server and the harness ran as the *same uid*. Same-uid is not a
boundary: whatever the server can read, a prompt-injected harness can read too, and the
container's configuration environment is handed to every process the runtime starts.

So the harness runs as its own uid. :mod:`ticket_to_pr.launch` forks this runner, drops it
to ``T2PR_HARNESS_UID``, and ``execve``\\s it with a scrubbed environment; the server keeps
uid 10001 and never gains the privilege to become anyone else. The two talk over an
inherited ``AF_UNIX`` socketpair.

What this buys, concretely
--------------------------
* ``/proc/<server>/environ`` and ``/proc/<server>/mem`` are unreadable to the harness uid.
* The server's ``$HOME`` is unwritable, so no ``usercustomize.py`` or ``.profile`` plant.
* A projected workload-identity token file readable only by the server uid is out of reach,
  which is the residual :mod:`ticket_to_pr.launch` could not close on its own.

This process is *also* exec'd with the scrubbed environment, and that matters: a forked
child inherits its parent's exec-time environ, so a fork alone would have left the whole
identity plumbing readable at ``/proc/<runner>/environ`` -- by a harness that shares this
very uid. The re-exec is what makes the runner's own environ clean.

The runner holds no credential of its own. Every request carries the exact environment the
harness is to receive, built by :func:`ticket_to_pr.harness.clean_env` on the server side.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys

#: Environment variable naming the inherited socket fd, on both sides of the pair.
RUNNER_FD_ENV = "T2PR_RUNNER_FD"

#: How long a killed process group gets to die politely before SIGKILL. Mirrors
#: :data:`ticket_to_pr.harness.KILL_GRACE_SECONDS`; duplicated rather than imported so the
#: runner has no import dependency on the harness package.
KILL_GRACE_SECONDS = 5.0

#: Frame header: one unsigned big-endian 32-bit length. Requests carry a prompt and
#: responses carry harness stdout, so frames are far larger than a socket write.
_HEADER = struct.Struct("!I")

#: Refuse absurd frames rather than allocating on a corrupted length. Harness output is
#: capped long before it reaches a PR body, but this is the wire boundary.
MAX_FRAME_BYTES = 64 * 1024 * 1024


def send_frame(sock: socket.socket, payload: dict[str, object]) -> None:
    """Write one length-prefixed JSON frame."""
    blob = json.dumps(payload).encode()
    sock.sendall(_HEADER.pack(len(blob)) + blob)


def _recv_exactly(sock: socket.socket, count: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None  # peer closed
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, object] | None:
    """Read one frame, or ``None`` when the peer has closed."""
    header = _recv_exactly(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"runner frame of {length} bytes exceeds the {MAX_FRAME_BYTES} cap")
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    decoded: dict[str, object] = json.loads(body)
    return decoded


def _terminate_group(proc: subprocess.Popen[str], pgid: int | None = None) -> None:
    """Kill the harness *and everything it started*, on **every** exit path.

    Identical in intent to the server-side helper: both CLIs spawn their own children, and
    ``subprocess`` only ever signals the direct child.

    This used to run only on ``TimeoutExpired``, so a clean exit signalled nothing. A
    harness that forks a detached child and then returns 0 therefore left that child alive
    in the container: the run reported success in under a second, and the survivor was
    still there minutes later writing into a *later* run's working directory -- past the
    point where ``changed_files`` counted the diff, so ``T2PR_MAX_FILES_CHANGED`` had
    already been spent on the run that is about to be pushed.

    ``pgid`` must be captured *before* the child is reaped: once ``communicate`` has waited
    on it, ``os.getpgid(proc.pid)`` raises and the group the survivors are still in becomes
    unaddressable. Callers pass the value they read right after ``Popen``.
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


def execute(request: dict[str, object]) -> dict[str, object]:
    """Run one harness CLI and describe what happened.

    Never raises: the server is on the other end of a socket and a traceback here would
    hang it. Every failure comes back as a ``status`` the server maps to a contract code.
    """
    raw_argv = request.get("argv")
    argv = [str(a) for a in raw_argv] if isinstance(raw_argv, list) else []
    raw_env = request.get("env")
    env = (
        {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    )
    cwd = str(request.get("cwd", ""))
    prompt = str(request.get("prompt", ""))
    raw_timeout = request.get("timeout_s")
    timeout_s = raw_timeout if isinstance(raw_timeout, int) else 0

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv built from validated settings
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return {"status": "start_failed", "message": exc.strerror or str(exc)}

    # Read while the child is still un-reaped; see _terminate_group.
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:  # pragma: no cover - the child died between Popen and here
        pgid = None

    try:
        with proc:
            try:
                stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _terminate_group(proc, pgid)
                with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
                    proc.communicate(timeout=KILL_GRACE_SECONDS)
                return {"status": "timeout"}
        return {
            "status": "ok",
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    finally:
        # Success, failure and timeout alike. Signalling a group that is already empty
        # raises ProcessLookupError, which _terminate_group suppresses, so the ordinary
        # case costs one failed syscall.
        _terminate_group(proc, pgid)


def serve(sock: socket.socket) -> int:
    """Answer run requests until the server closes the socket."""
    # Group-writable so the server uid can read back, modify and delete what the harness
    # wrote into the shared run directory. The directory itself carries the setgid bit, so
    # new files land in the shared group (see ticket_to_pr.run).
    os.umask(0o002)
    while True:
        try:
            request = recv_frame(sock)
        except (OSError, ValueError, json.JSONDecodeError):
            return 1
        if request is None:
            return 0
        response = execute(request)
        try:
            send_frame(sock, response)
        except OSError:
            return 1


def main() -> int:
    """Runner entrypoint: ``python -m ticket_to_pr.runner``, exec'd by the launcher."""
    raw = os.environ.pop(RUNNER_FD_ENV, None)
    if raw is None:
        print(
            "ticket-to-pr runner: no socket to serve; not meant to be run directly",
            file=sys.stderr,
        )
        return 2
    sock = socket.socket(fileno=int(raw))
    with sock:
        return serve(sock)


if __name__ == "__main__":
    raise SystemExit(main())
