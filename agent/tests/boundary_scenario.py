"""The uid-boundary scenario, run as a subprocess by ``test_runner.py``.

A separate file rather than an inline string: dropping privileges is irreversible, so it
cannot happen inside the pytest process without poisoning every later test, and the payload
the harness executes is itself Python, which makes nested quoting unreadable.

Run as root on Linux. Prints one JSON object describing what a harness can reach.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ["T2PR_SRC"])

from ticket_to_pr import launch
from ticket_to_pr.runner import recv_frame, send_frame

# The payload the harness runs. Probes each route a security finding used.
#
# Two parents, not one. ``/proc/<server>/environ`` is the cross-uid read the uid split
# closed. ``/proc/<runner>/environ`` is the *same-uid* read it cannot close: the runner is
# this process's parent and wears uid 10002 itself, so /proc permits the read and the only
# defence is that the launcher never put a secret in the environment it exec'd the runner
# with. That is the route the Azure DevOps PAT leaked down -- ``T2PR_ADO_PAT`` matched no
# fragment in ``launch.CREDENTIAL_NAME_FRAGMENTS``, so it survived into ``child_env`` and
# from there into the runner's exec-time environ, readable by every CLI the runner spawns.
HARNESS_PROBE = """
import json, os, sys
server_pid = int(sys.argv[1])
d = {"uid": os.getuid(), "runner_pid": os.getppid()}
try:
    blob = open(f"/proc/{server_pid}/environ", "rb").read()
    d["server_environ"] = "readable"
    d["sentinel"] = b"SENTINEL-MI-HEADER" in blob
except Exception:
    d["server_environ"] = "denied"
    d["sentinel"] = False
try:
    blob = open(f"/proc/{os.getppid()}/environ", "rb").read()
    d["runner_environ"] = "readable"
    d["runner_pat_sentinel"] = b"SENTINEL-ADO-PAT" in blob
    d["runner_mi_sentinel"] = b"SENTINEL-MI-HEADER" in blob
except Exception:
    d["runner_environ"] = "denied"
    d["runner_pat_sentinel"] = False
    d["runner_mi_sentinel"] = False
try:
    open("/work/.profile", "w").write("x")
    d["server_home"] = "writable"
except Exception:
    d["server_home"] = "denied"
try:
    open(f"/proc/{server_pid}/mem", "rb").read(1)
    d["server_mem"] = "readable"
except Exception:
    d["server_mem"] = "denied"
print(json.dumps(d))
"""


def main() -> int:
    os.environ["IDENTITY_HEADER"] = "SENTINEL-MI-HEADER"
    # The other secret this container holds by value. Its name matches no *identity*
    # fragment, which is precisely why it needs its own sentinel: a probe that only looks
    # for IDENTITY_HEADER passes for a reason that does not generalise (deploy.md §3).
    os.environ["T2PR_ADO_PAT"] = "SENTINEL-ADO-PAT"
    sock = launch._spawn_runner(launch.scrubbed_environ(dict(os.environ)))
    if sock is None:
        print(json.dumps({"skip": "not privileged enough to drop"}))
        return 0

    launch._drop_privileges(
        launch.SERVER_UID, launch.SERVER_GID, extra_groups=(launch.SHARED_GID,)
    )
    server_pid = os.getpid()

    work = tempfile.mkdtemp(prefix="t2pr-boundary-")
    os.chown(work, -1, launch.SHARED_GID)
    # Mirrors run._make_run_dir: setgid + group-write is what lets the harness uid write
    # here and the server read it back. Two-member group, deleted at the end of the run.
    Path(work).chmod(0o2770)

    send_frame(
        sock,
        {
            "argv": [sys.executable, "-I", "-c", HARNESS_PROBE, str(server_pid)],
            "cwd": work,
            "env": {"PATH": os.environ["PATH"], "HOME": work},
            "prompt": "",
            "timeout_s": 60,
        },
    )
    response = recv_frame(sock)
    if response is None or response.get("status") != "ok":
        print(json.dumps({"error": f"runner returned {response}"}))
        return 1

    findings = json.loads(str(response["stdout"]).strip().splitlines()[-1])
    findings["server_uid"] = launch.SERVER_UID
    print(json.dumps(findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
