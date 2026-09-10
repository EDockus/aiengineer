"""The unprivileged harness runner and the server's client half.

Security review, third round trip: the harness runs as its own uid so it cannot read the
server's ``/proc`` entries, write the server's ``$HOME``, or reach a projected
workload-identity token. These tests pin the wire protocol and the failure mapping, which
is the part that runs on every platform. The uid boundary itself needs a Linux container
and is covered by ``test_the_harness_uid_is_a_real_boundary`` below, which skips elsewhere.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ticket_to_pr.harness import _run_via_runner, run_cli
from ticket_to_pr.models import ErrorCode, RunFailure
from ticket_to_pr.runner import (
    MAX_FRAME_BYTES,
    execute,
    recv_frame,
    send_frame,
)


@pytest.fixture
def pair() -> tuple[socket.socket, socket.socket]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    yield a, b
    a.close()
    b.close()


# ------------------------------------------------------------------------------- framing


def _send_async(sock: socket.socket, payload: dict[str, object]) -> threading.Thread:
    """Send on a thread.

    A socketpair holds only a few kilobytes; ``sendall`` of a realistic frame blocks until
    the peer drains it. In the running system the runner is always parked in ``recv_frame``
    so it drains continuously, but a test that sends and then reads on one thread would
    deadlock on itself.
    """
    thread = threading.Thread(target=send_frame, args=(sock, payload), daemon=True)
    thread.start()
    return thread


def test_a_frame_survives_the_round_trip(pair: tuple[socket.socket, socket.socket]) -> None:
    server, runner = pair
    payload = {"argv": ["echo", "hi"], "prompt": "x" * 100_000, "timeout_s": 5}
    thread = _send_async(server, payload)
    assert recv_frame(runner) == payload
    thread.join(timeout=5)


def test_a_closed_peer_reads_as_none(pair: tuple[socket.socket, socket.socket]) -> None:
    """The runner's exit condition: the server closing the socket, not an error."""
    server, runner = pair
    server.close()
    assert recv_frame(runner) is None


def test_a_frame_split_across_reads_is_reassembled(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Harness stdout is far larger than a socket buffer, so short reads are the norm."""
    server, runner = pair
    payload = {"stdout": "y" * (2 * 1024 * 1024)}
    thread = _send_async(server, payload)
    assert recv_frame(runner) == payload
    thread.join(timeout=10)


def test_an_absurd_frame_length_is_refused(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Refuse rather than allocate on a corrupted length."""
    import struct

    server, runner = pair
    server.sendall(struct.pack("!I", MAX_FRAME_BYTES + 1) + b"{}")
    with pytest.raises(ValueError, match="exceeds"):
        recv_frame(runner)


# ------------------------------------------------------------------------------- execute


def test_execute_reports_a_successful_run() -> None:
    result = execute(
        {
            "argv": [sys.executable, "-I", "-c", "import sys; print(sys.stdin.read().strip())"],
            "cwd": str(Path.cwd()),
            "env": {"PATH": os.environ["PATH"]},
            "prompt": "hello from the prompt",
            "timeout_s": 30,
        }
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "hello from the prompt" in str(result["stdout"])


def test_execute_reports_a_nonzero_exit_without_raising() -> None:
    result = execute(
        {
            "argv": [sys.executable, "-I", "-c", "import sys; sys.exit(3)"],
            "cwd": str(Path.cwd()),
            "env": {"PATH": os.environ["PATH"]},
            "prompt": "",
            "timeout_s": 30,
        }
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 3


def test_execute_reports_a_missing_binary_rather_than_raising() -> None:
    """A traceback here would hang the server, which is blocked on the response frame."""
    result = execute(
        {
            "argv": ["/nonexistent/harness-cli"],
            "cwd": str(Path.cwd()),
            "env": {"PATH": os.environ["PATH"]},
            "prompt": "",
            "timeout_s": 30,
        }
    )
    assert result["status"] == "start_failed"
    assert result["message"]


def test_execute_times_out_and_leaves_no_live_descendants(tmp_path) -> None:
    """The runner owns the process group, so it is the only party that can kill the tree."""
    marker = tmp_path / "grandchild.txt"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', "
        f"\"import time\\nwhile True:\\n open(r'{marker}','a').write('x')\\n time.sleep(0.1)\"])\n"
        "time.sleep(60)\n"
    )
    result = execute(
        {
            "argv": [sys.executable, "-I", "-c", script],
            "cwd": str(tmp_path),
            "env": {"PATH": os.environ["PATH"]},
            "prompt": "",
            "timeout_s": 2,
        }
    )
    assert result["status"] == "timeout"
    time.sleep(0.5)
    size_after_kill = marker.stat().st_size if marker.exists() else 0
    time.sleep(1.0)
    final = marker.stat().st_size if marker.exists() else 0
    assert final == size_after_kill, "a grandchild outlived the timeout and kept writing"


# ---------------------------------------------------- descendants of a *successful* run


def _detached_writer(tmp_path: Path) -> tuple[list[str], Path]:
    """A harness that forks a detached child, closes its stdio and exits 0 immediately.

    The child writes into whatever directory the beacon file names, so moving the beacon
    after the run models exactly the escalation: a process left over from run A editing
    run B's clone, *after* ``changed_files`` counted run B's diff, so
    ``T2PR_MAX_FILES_CHANGED`` has already been spent and the attacker's content is
    committed and pushed into somebody else's pull request.

    The child's stdout and stderr go to ``/dev/null`` on purpose. Left inherited they hold
    the pipes open and ``communicate`` blocks until the child is done -- which would hide
    the bug behind a slow run rather than a leaked process.
    """
    beacon = tmp_path / "beacon"
    child = tmp_path / "survivor_child.py"
    child.write_text(
        "import os, sys, time\n"
        "for _ in range(300):\n"
        "    try:\n"
        "        target = open(sys.argv[1]).read().strip()\n"
        "        open(os.path.join(target, 'INJECTED.txt'), 'w').write('from run A')\n"
        "    except OSError:\n"
        "        pass\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    harness = tmp_path / "survivor_harness.py"
    harness.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '-I', sys.argv[1], sys.argv[2]],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return [sys.executable, "-I", str(harness), str(child), str(beacon)], beacon


def _next_run_dir_stays_clean(tmp_path: Path, beacon: Path) -> bool:
    """Point the beacon at a directory created *after* the run and see if anything writes."""
    run_b = tmp_path / "run-b"
    run_b.mkdir()
    beacon.write_text(str(run_b), encoding="utf-8")
    time.sleep(1.5)
    return not (run_b / "INJECTED.txt").exists()


def test_execute_leaves_no_live_descendants_after_a_successful_run(tmp_path: Path) -> None:
    """The timeout path was covered; the *success* path signalled nothing at all.

    ``_terminate_group`` used to run only on ``TimeoutExpired``, which made "return 0
    quickly" the cheapest way to leave a process behind: the run reported success in a
    fraction of a second and the survivor was still writing minutes later.
    """
    run_a = tmp_path / "run-a"
    run_a.mkdir()
    argv, beacon = _detached_writer(tmp_path)
    beacon.write_text(str(run_a), encoding="utf-8")

    result = execute(
        {
            "argv": argv,
            "cwd": str(run_a),
            "env": {"PATH": os.environ["PATH"]},
            "prompt": "",
            "timeout_s": 30,
        }
    )

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert _next_run_dir_stays_clean(tmp_path, beacon), (
        "a descendant outlived a successful run and wrote into the next run's directory"
    )


def test_run_cli_leaves_no_live_descendants_after_a_successful_run(tmp_path: Path) -> None:
    """The same property for the in-process fallback, which is the path the tests, dev
    mode and any non-root container take."""
    run_a = tmp_path / "run-a"
    run_a.mkdir()
    argv, beacon = _detached_writer(tmp_path)
    beacon.write_text(str(run_a), encoding="utf-8")

    completed = run_cli(
        argv, repo_dir=run_a, prompt="", timeout_s=30, env={"PATH": os.environ["PATH"]}
    )

    assert completed.returncode == 0
    assert _next_run_dir_stays_clean(tmp_path, beacon), (
        "a descendant outlived a successful run in the in-process fallback"
    )


# -------------------------------------------------------------- the server's client half


def _serving_pair() -> tuple[socket.socket, subprocess.Popen[bytes]]:
    """A socket whose far end is a real runner loop in a separate process."""
    server, runner = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(runner.fileno(), True)
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import os, socket, sys;"
         "sys.path.insert(0, os.environ['T2PR_SRC']);"
         "from ticket_to_pr.runner import serve;"
         f"serve(socket.socket(fileno={runner.fileno()}))"],
        env={**os.environ, "T2PR_SRC": str(_src_dir())},
        pass_fds=(runner.fileno(),),
        close_fds=True,
    )
    runner.close()
    return server, proc


def _src_dir() -> str:
    import ticket_to_pr

    return str(Path(ticket_to_pr.__file__).parent.parent)


def test_the_client_maps_a_timeout_to_the_contract_code(tmp_path) -> None:
    server, proc = _serving_pair()
    try:
        with pytest.raises(RunFailure) as excinfo:
            _run_via_runner(
                server,
                [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                repo_dir=tmp_path,
                prompt="",
                timeout_s=2,
                env={"PATH": os.environ["PATH"]},
            )
        assert excinfo.value.code is ErrorCode.TIMEOUT
    finally:
        server.close()
        proc.wait(timeout=10)


def test_the_client_maps_a_missing_binary_to_harness_failed(tmp_path) -> None:
    server, proc = _serving_pair()
    try:
        with pytest.raises(RunFailure) as excinfo:
            _run_via_runner(
                server,
                ["/nonexistent/harness-cli"],
                repo_dir=tmp_path,
                prompt="",
                timeout_s=10,
                env={"PATH": os.environ["PATH"]},
            )
        assert excinfo.value.code is ErrorCode.HARNESS_FAILED
    finally:
        server.close()
        proc.wait(timeout=10)


def test_the_client_returns_a_completed_process_on_success(tmp_path) -> None:
    server, proc = _serving_pair()
    try:
        completed = _run_via_runner(
            server,
            [sys.executable, "-I", "-c", "print('from the runner')"],
            repo_dir=tmp_path,
            prompt="",
            timeout_s=30,
            env={"PATH": os.environ["PATH"]},
        )
        assert completed.returncode == 0
        assert "from the runner" in completed.stdout
    finally:
        server.close()
        proc.wait(timeout=10)


def test_a_dead_runner_becomes_harness_failed_not_a_traceback(tmp_path) -> None:
    """If the runner dies the run must fail with a contract code, not an unhandled error."""
    server, runner = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    runner.close()  # nobody is serving
    try:
        with pytest.raises(RunFailure) as excinfo:
            _run_via_runner(
                server,
                ["echo", "hi"],
                repo_dir=tmp_path,
                prompt="",
                timeout_s=5,
                env={},
            )
        assert excinfo.value.code is ErrorCode.HARNESS_FAILED
    finally:
        server.close()


# ------------------------------------------------------------------- the uid boundary

pytestmark_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.getuid() != 0,
    reason="needs Linux and root to drop privileges; runs in the container and in CI",
)


@pytestmark_linux
def test_the_harness_uid_is_a_real_boundary() -> None:
    """The property the whole design exists for.

    A harness must not be able to read the server's environment or memory out of
    ``/proc``, nor write into the server's ``$HOME``. Same-uid gave all three away, and
    three security findings in a row exploited exactly that. Runs in the built image and
    in CI; skipped on macOS, which has neither ``/proc`` nor a second account.

    The scenario lives in ``tests/boundary_scenario.py`` and runs as a subprocess: it has
    to drop privileges, which is irreversible and would poison every later test here.

    **The runner's own environ is the half a uid boundary cannot defend.** The runner is
    the harness's parent and wears the harness's uid, so ``/proc/<runner>/environ`` is a
    permitted same-uid read no matter how the accounts are arranged. The only thing
    standing there is that :mod:`ticket_to_pr.launch` never put a secret in the environment
    it exec'd the runner with. ``T2PR_ADO_PAT`` matched no fragment in
    ``CREDENTIAL_NAME_FRAGMENTS``, so for four review rounds it did -- and every coding CLI
    the runner spawned could read an org-wide ``vso.work vso.code_write`` token out of its
    parent's environ. Asserting only on the *server's* environ passed that whole time.
    """
    scenario = Path(__file__).parent / "boundary_scenario.py"
    completed = subprocess.run(
        [sys.executable, str(scenario)],
        env={**os.environ, "T2PR_SRC": _src_dir()},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    findings = json.loads(completed.stdout.strip().splitlines()[-1])
    if "skip" in findings:
        pytest.skip(str(findings["skip"]))
    assert "error" not in findings, findings

    assert findings["uid"] != findings["server_uid"], "the harness shares the server's uid"
    assert findings["server_environ"] == "denied"
    assert findings["sentinel"] is False
    assert findings["server_home"] == "denied"
    assert findings["server_mem"] == "denied"

    # The runner. Its environ IS readable -- same uid, and that is expected, not a defect;
    # what matters is that there is nothing in it worth reading. Asserting the read is
    # permitted keeps the next two assertions honest: if /proc ever stopped answering, they
    # would pass for the wrong reason.
    assert findings["runner_environ"] == "readable", (
        "the probe could not read the runner's environ, so the assertions below prove "
        "nothing -- fix the probe rather than trusting this result"
    )
    assert findings["runner_pat_sentinel"] is False, (
        "a coding CLI recovered the Azure DevOps PAT from /proc/<runner>/environ"
    )
    assert findings["runner_mi_sentinel"] is False
