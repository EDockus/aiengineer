"""The exec-time environment scrub (security review HIGH-1, second round trip; CRITICAL,
fifth).

``harness.clean_env`` keeps credentials out of a harness's *own* environment, but
``/proc/<pid>/environ`` is a process's **exec-time** environment -- unchanged by anything
that process does to ``os.environ`` afterwards -- and the kernel lets any process read it
for a pid at its own uid. :mod:`ticket_to_pr.launch` is the container ENTRYPOINT that
closes that: it scrubs credential-shaped variables out of the environment it exec's the
server *and the runner* with, and re-delivers them to the server alone on an inherited pipe
fd it drains at startup.

**Two secrets, not one.** The managed-identity plumbing was the first; the Azure DevOps
PAT (``T2PR_ADO_PAT``, docs/deploy.md §3) is the second, and it went unscrubbed for four
review rounds because ``CREDENTIAL_NAME_FRAGMENTS`` named no fragment that matched it. It
is the worse leak of the two: long-lived, org-wide, valid until revoked. So every property
below is asserted for both, and the fragment lists are pinned against each other so the
next variable cannot fall through the same gap silently.

Three levels of test:

* the two fragment lists, pinned to each other and checked against the allowlist;
* the pure partition/round-trip logic, which runs everywhere; and
* the property that actually matters -- a ``run_cli`` child cannot recover a scrubbed
  sentinel from its parent's ``/proc/<pid>/environ`` -- which needs ``/proc`` and so is
  skipped off Linux (e.g. a macOS dev host); it exercises the real thing in the container
  and in CI. ``test_runner.py::test_the_harness_uid_is_a_real_boundary`` covers the same
  read against the *runner*, which is where the PAT actually leaked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import ticket_to_pr
from ticket_to_pr.harness import ENV_ALLOWLIST, FORBIDDEN_ENV_FRAGMENTS
from ticket_to_pr.launch import (
    CRED_FD_ENV,
    CREDENTIAL_NAME_FRAGMENTS,
    absorb_credentials,
    collect_credentials,
    scrubbed_environ,
)

SENTINEL = "SENTINEL-VALUE-THAT-MUST-NOT-ESCAPE"

#: The Azure DevOps Personal Access Token (docs/deploy.md §3). Not identity plumbing --
#: a long-lived org-wide secret this container holds by value -- and it needs its own
#: sentinel because its name matches none of the *identity* fragments. It once matched none
#: of the credential fragments either, which is how it reached the runner's exec-time
#: environment at the harness's own uid.
PAT_SENTINEL = "SENTINEL-ADO-PAT-THAT-MUST-NOT-ESCAPE"

#: The identity/credential plumbing a Container Apps / App Service / workload-identity
#: deployment injects into the container's initial process.
IDENTITY_ENV = {
    "IDENTITY_ENDPOINT": "http://127.0.0.1:42356/msi/token",
    "IDENTITY_HEADER": SENTINEL,
    "MSI_SECRET": SENTINEL,
    "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_TENANT_ID": "22222222-2222-2222-2222-222222222222",
    "AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token",
}

# --------------------------------------------------------- the two lists, pinned together


def test_the_two_fragment_lists_are_the_same_list() -> None:
    """launch.py claims its list is identical to harness.FORBIDDEN_ENV_FRAGMENTS. This is
    what makes that claim enforceable rather than aspirational -- the previous version of
    the claim was a comment saying "deliberately in step with", and the two had already
    drifted apart by ``DEVOPS`` when it was written.

    The two do the same job at different boundaries -- one decides what a harness may
    inherit, the other what may sit in any process's exec-time environment where
    ``/proc/<pid>/environ`` exposes it to the same uid. A name in one and not the other is
    a variable defended on one hop and naked on the next, which is precisely what happened
    to ``T2PR_ADO_PAT``: it was absent from both, and the lists differed by ``DEVOPS``, so
    nothing pointed at the gap.
    """
    assert CREDENTIAL_NAME_FRAGMENTS == FORBIDDEN_ENV_FRAGMENTS


def test_no_fragment_would_strip_an_allowlisted_variable() -> None:
    """Matching is a substring test, so a careless fragment silently breaks the container.

    ``PAT`` is the specific trap: ``PATH`` contains it, and a runner exec'd without ``PATH``
    cannot spawn anything at all. The fragment has to be ``ADO_PAT``.
    """
    for name in ENV_ALLOWLIST:
        for fragment in CREDENTIAL_NAME_FRAGMENTS:
            assert fragment not in name.upper(), (
                f"fragment {fragment!r} matches allowlisted {name!r}: "
                "it would be stripped from every subprocess environment"
            )


# ------------------------------------------------------------------ the partition, pure


def test_the_ado_pat_is_scrubbed_off_the_exec_time_environment() -> None:
    """The CRITICAL, at the unit level.

    ``scrubbed_environ`` builds what ``launch.main`` execs BOTH the server and the runner
    with, and the runner has already dropped to the harness's own uid -- so anything left
    here is readable by every coding CLI through ``/proc/<runner>/environ``, with no uid
    boundary in the way. A PAT is worse there than a managed-identity header: it is
    long-lived, org-wide ``vso.work vso.code_write``, and valid until somebody revokes it.
    """
    source = {"PATH": "/usr/bin", "T2PR_ADO_PAT": PAT_SENTINEL, "T2PR_ALLOWED_ORGS": "contoso"}

    assert "T2PR_ADO_PAT" not in scrubbed_environ(source)
    assert PAT_SENTINEL not in scrubbed_environ(source).values()
    # ...and it still reaches the server, which needs it for Settings.from_env.
    assert collect_credentials(source)["T2PR_ADO_PAT"] == PAT_SENTINEL
    # Non-secret configuration is untouched: over-scrubbing would break startup.
    assert scrubbed_environ(source)["T2PR_ALLOWED_ORGS"] == "contoso"


def test_path_survives_the_scrub() -> None:
    """The reason the fragment is ADO_PAT and not PAT, asserted where it would bite."""
    assert scrubbed_environ({"PATH": "/usr/bin:/bin"})["PATH"] == "/usr/bin:/bin"


def test_collect_and_scrub_partition_the_environment() -> None:
    source = {"PATH": "/usr/bin", "T2PR_ALLOWED_ORGS": "contoso", **IDENTITY_ENV}
    creds = collect_credentials(source)
    kept = scrubbed_environ(source)

    # Everything credential-shaped is collected...
    assert set(creds) == set(IDENTITY_ENV)
    # ...and nothing credential-shaped survives in the environment the server is exec'd with.
    assert set(kept) == {"PATH", "T2PR_ALLOWED_ORGS"}
    assert not any(v == SENTINEL for v in kept.values())


def test_the_handoff_fd_marker_is_not_treated_as_a_credential() -> None:
    # The fd number is a small integer, not a secret: it must pass through untouched or the
    # server could never find the pipe.
    source = {CRED_FD_ENV: "3", "IDENTITY_HEADER": SENTINEL}
    assert CRED_FD_ENV not in collect_credentials(source)
    assert scrubbed_environ(source)[CRED_FD_ENV] == "3"


def test_absorb_round_trips_the_values_off_a_pipe_and_closes_it() -> None:
    creds = {"IDENTITY_HEADER": SENTINEL, "MSI_SECRET": "another"}
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"\0".join(f"{k}={v}".encode() for k, v in creds.items()))
    os.close(write_fd)
    env: dict[str, str] = {CRED_FD_ENV: str(read_fd)}

    absorb_credentials(env)

    assert env["IDENTITY_HEADER"] == SENTINEL
    assert env["MSI_SECRET"] == "another"
    assert CRED_FD_ENV not in env  # marker consumed
    with pytest.raises(OSError):  # fd closed
        os.fstat(read_fd)


def test_absorb_is_a_noop_without_the_marker() -> None:
    env = {"PATH": "/usr/bin"}
    absorb_credentials(env)
    assert env == {"PATH": "/usr/bin"}


# ---------------------------------------------------- the /proc property (Linux/container)

#: A launcher+server+harness in one file. Run with no ``ROLE`` it plays the launcher
#: (``launch.main``'s exact shape, using the real functions), exec'ing itself as the
#: server with the identity plumbing scrubbed and handed off on a pipe. As the server it
#: drains the fd, then spawns a harness child through the real ``run_cli``/``clean_env``;
#: the child reads the server's exec-time environ from ``/proc`` and reports the sentinel.
_SCENARIO = r'''
import os, sys
from pathlib import Path
import ticket_to_pr.launch as launch
from ticket_to_pr.harness import clean_env, run_cli

if os.environ.get("ROLE") != "server":
    source = dict(os.environ)
    creds = launch.collect_credentials(source)
    child_env = launch.scrubbed_environ(source)
    if creds:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, launch._encode(creds))
        os.close(write_fd)
        os.set_inheritable(read_fd, True)
        child_env[launch.CRED_FD_ENV] = str(read_fd)
    child_env["ROLE"] = "server"
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], child_env)

launch.absorb_credentials()
# The sentinel is recovered ONLY through the fd handoff (os.environ), never from any
# surviving variable -- so if the child finds it in the parent's /proc environ, the scrub
# leaked. The harness child reads that environ and reports whether it reached the sentinel.
sentinel = os.environ["MSI_SECRET"]
pat = os.environ["T2PR_ADO_PAT"]
payload = "\n".join([
    "import os",
    "env = open(f'/proc/{os.getppid()}/environ', 'rb').read().decode('latin1')",
    "import sys",
    "print('CHILD_SEES_SENTINEL', sys.argv[1] in env)",
    "print('CHILD_SEES_PAT', sys.argv[2] in env)",
])
proc = run_cli([sys.executable, "-c", payload, sentinel, pat], repo_dir=Path(os.getcwd()),
               prompt="", timeout_s=30, env=clean_env())
sys.stdout.write(proc.stdout)
print("SERVER_RECOVERED_SENTINEL", os.environ.get("MSI_SECRET") == sentinel)
print("SERVER_RECOVERED_PAT", os.environ.get("T2PR_ADO_PAT") == pat)
'''


@pytest.mark.skipif(
    sys.platform != "linux" or not Path("/proc/self/environ").exists(),
    reason="needs /proc; exercised in the container image and in Linux CI, not on macOS",
)
def test_run_cli_child_cannot_read_a_scrubbed_sentinel_from_proc(tmp_path: Path) -> None:
    """docs/architecture.md §6, defended against the /proc route (SECURITY.md,
    "Keeping the identity out of the harness's *reach*")."""
    script = tmp_path / "scenario.py"
    script.write_text(_SCENARIO, encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(ticket_to_pr.__file__).resolve().parent.parent),
        **IDENTITY_ENV,  # the sentinel lives ONLY in credential-shaped vars, all scrubbed
        "T2PR_ADO_PAT": PAT_SENTINEL,
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
        cwd=str(tmp_path),
    )

    # The harness child, reading the server's exec-time environ, must not find either.
    assert "CHILD_SEES_SENTINEL False" in result.stdout, result.stdout + result.stderr
    assert "CHILD_SEES_PAT False" in result.stdout, result.stdout + result.stderr
    # But the server still has both, so inference and Azure DevOps both keep working.
    assert "SERVER_RECOVERED_SENTINEL True" in result.stdout, result.stdout + result.stderr
    assert "SERVER_RECOVERED_PAT True" in result.stdout, result.stdout + result.stderr
