"""Shared fixtures for the shipped test suite (docs/architecture.md §7).

Nothing here reaches a live model or a live Azure DevOps organization:

* Azure DevOps is served by :class:`FakeAdo`, an ``httpx.MockTransport`` that replays
  the recorded response bodies in ``tests/fixtures/ado/`` through the *real*
  ``ticket_to_pr.ado.AdoClient``.
* The git remote is a genuine bare repository on disk reached over ``file://``, so
  ``git_ops.clone``/``push``/``delete`` do real work and a "the branch is gone from the
  remote" assertion means what it says.
* The coding step is ``FakeHarness`` (or a deliberately broken variant of it).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from ticket_to_pr import ado
from ticket_to_pr.config import AuthMode, HarnessName, Settings
from ticket_to_pr.harness import HarnessResult
from ticket_to_pr.harness.fake import FakeHarness
from ticket_to_pr.models import InvocationRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ado"

ORG = "contoso"
PROJECT = "Platform"
REPO_NAME = "payments-api"
REPO_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_BRANCH = "main"
TARGET_BRANCH = "bugfix/4123-null-ref"
ZERO = "0" * 40

#: A base URL that is syntactically a real ADO org URL but resolves nowhere: every
#: request in these tests is answered by MockTransport, never by the network.
ADO_BASE_URL = "https://ado.invalid"


def fixture(name: str) -> Any:
    """Load one recorded Azure DevOps response body."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------------------


BASE_SETTINGS = Settings(
    harness=HarnessName.FAKE,
    model_deployment="gpt-6",
    allowed_orgs=frozenset({ORG.casefold()}),
    max_run_minutes=20,
    max_files_changed=50,
    pr_draft=True,
    pr_reviewers=(),
    commit_author_name="Ticket-to-PR agent",
    commit_author_email="ticket-to-pr@noreply.local",
    log_level="INFO",
    host="127.0.0.1",
    port=8000,
    ado_base_url=ADO_BASE_URL,
    auth_mode=AuthMode.DEV_STATIC,
    dev_mode=True,
    ado_pat="",
)


def make_settings(**overrides: Any) -> Settings:
    """A valid :class:`Settings` with the fake harness, plus any field overrides."""
    return replace(BASE_SETTINGS, **overrides)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# --------------------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------------------


def git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(cwd or Path.cwd()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Test Seed",
            "GIT_AUTHOR_EMAIL": "seed@test.invalid",
            "GIT_COMMITTER_NAME": "Test Seed",
            "GIT_COMMITTER_EMAIL": "seed@test.invalid",
        },
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def seed_bare_repo(bare: Path, *, branch: str = DEFAULT_BRANCH) -> Path:
    """Create a real bare repository with one commit, usable as a ``file://`` remote."""
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "--initial-branch", branch, str(bare)])
    work = bare.parent / f"{bare.stem}-seed"
    git(["clone", f"file://{bare}", str(work)])
    (work / "README.md").write_text("# payments-api\n", encoding="utf-8")
    (work / "app.py").write_text("def charge(amount: int) -> int:\n    return amount\n", "utf-8")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "Initial commit"], cwd=work)
    git(["push", "origin", branch], cwd=work)
    git(["config", "receive.denyDeletes", "false"], cwd=bare)
    git(["config", "receive.denyCurrentBranch", "ignore"], cwd=bare)
    return bare


def remote_branches(bare: Path) -> dict[str, str]:
    out = git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], cwd=bare)
    result: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.partition(" ")
        if name:
            result[name] = sha.strip()
    return result


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    return seed_bare_repo(tmp_path / "remote" / "payments-api.git")


# --------------------------------------------------------------------------------------
# the recorded Azure DevOps surface
# --------------------------------------------------------------------------------------

_PATH_RE = re.compile(
    rf"^/{ORG}/(?P<project>[^/]+)/_apis/(?P<rest>.+)$",
)


class FakeAdo:
    """Replays ``tests/fixtures/ado/`` through ``httpx.MockTransport``.

    ``force`` maps a route name to an HTTP status the route should return instead of its
    recorded body, which is how the failure-path tests reach a specific error code
    without any live service.
    """

    ROUTES = (
        "work_item_types",
        "work_item",
        "repository",
        "list_refs",
        "update_refs",
        "pull_request",
    )

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.force: dict[str, int] = {}
        #: Raise this instead of answering, to exercise the transport-failure path.
        self.transport_error: Exception | None = None
        #: Answer every call with this instead, e.g. ADO's 203 HTML sign-in page.
        self.raw_response: httpx.Response | None = None
        #: Override the ``remoteUrl`` the repository fixture reports.
        self.remote_url_override: str | None = None
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []
        self.auth_headers: list[str] = []
        #: Branches the refs endpoint should claim exist beyond what the bare repo has.
        self.extra_branches: dict[str, str] = {}
        self.pr_payload = fixture("pullrequest-created.json")
        self.transport = httpx.MockTransport(self._handle)
        self.client = httpx.AsyncClient(transport=self.transport)

    # -- routing ------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.transport_error is not None:
            raise self.transport_error
        if self.raw_response is not None:
            return self.raw_response
        self.auth_headers.append(request.headers.get("authorization", ""))
        body: Any = None
        if request.content:
            body = json.loads(request.content)
        self.bodies.append(body)

        match = _PATH_RE.match(request.url.path)
        if match is None:
            return self._json(404, fixture("error-404-workitem.json"))
        rest = match.group("rest")
        route, payload = self._dispatch(request, rest)
        if route is None:
            return self._json(404, {"message": f"no fake route for {rest}"})
        forced = self.force.get(route)
        if forced is not None:
            return self._json(forced, fixture("error-403.json"))
        return payload

    def _dispatch(
        self, request: httpx.Request, rest: str
    ) -> tuple[str | None, httpx.Response]:
        if rest == "wit/workitemtypes" and request.method == "GET":
            return "work_item_types", self._json(200, fixture("workitemtypes.json"))

        m = re.fullmatch(r"wit/workitems/(\d+)", rest)
        if m and request.method == "GET":
            return "work_item", self._work_item(int(m.group(1)))

        m = re.fullmatch(r"git/repositories/([^/]+)", rest)
        if m and request.method == "GET":
            return "repository", self._repository(m.group(1))

        m = re.fullmatch(r"git/repositories/([^/]+)/refs", rest)
        if m and request.method == "GET":
            return "list_refs", self._list_refs(request)
        if m and request.method == "POST":
            return "update_refs", self._update_refs(request)

        m = re.fullmatch(r"git/repositories/([^/]+)/pullrequests", rest)
        if m and request.method == "POST":
            return "pull_request", self._create_pr(request)

        return None, self._json(404, {"message": "unknown route"})

    # -- routes -------------------------------------------------------------------

    def _work_item(self, work_item_id: int) -> httpx.Response:
        path = FIXTURE_DIR / f"workitem-{work_item_id}.json"
        if not path.exists():
            return self._json(404, fixture("error-404-workitem.json"))
        return self._json(200, json.loads(path.read_text(encoding="utf-8")))

    def _repository(self, name: str) -> httpx.Response:
        if name not in (REPO_NAME, REPO_ID):
            return self._json(404, {"message": f"repository {name!r} does not exist"})
        data = fixture("repository.json")
        data["remoteUrl"] = self.remote_url_override or f"file://{self.bare}"
        return self._json(200, data)

    def _list_refs(self, request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get("filter", "")
        prefix = f"refs/{wanted}" if wanted else "refs/heads/"
        known = dict(remote_branches(self.bare))
        known.update(self.extra_branches)
        value = [
            {"name": name, "objectId": sha, "creator": {"displayName": "seed"}}
            for name, sha in known.items()
            if name == prefix or name.startswith(prefix)
        ]
        return self._json(200, {"count": len(value), "value": value})

    def _update_refs(self, request: httpx.Request) -> httpx.Response:
        results = []
        for item in json.loads(request.content):
            name = str(item.get("name", ""))
            new_object_id = str(item.get("newObjectId", ""))
            if new_object_id == ZERO:
                git(["update-ref", "-d", name], cwd=self.bare)
            else:
                git(["update-ref", name, new_object_id], cwd=self.bare)
            results.append({"name": name, "success": True, "updateStatus": "succeeded"})
        return self._json(200, {"count": len(results), "value": results})

    def _create_pr(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        source = str(body.get("sourceRefName", ""))
        if source not in remote_branches(self.bare):
            # Azure DevOps rejects a PR whose source ref was never pushed.
            return self._json(404, {"message": f"source ref {source} does not exist"})
        payload = dict(self.pr_payload)
        payload["title"] = body.get("title")
        payload["description"] = body.get("description")
        payload["sourceRefName"] = source
        payload["targetRefName"] = body.get("targetRefName")
        payload["isDraft"] = body.get("isDraft")
        payload["reviewers"] = body.get("reviewers", [])
        return self._json(201, payload)

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _json(status: int, payload: Any) -> httpx.Response:
        return httpx.Response(
            status,
            json=payload,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    def sent(self, method: str, needle: str) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method == method and needle in str(r.url)
        ]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every ``AdoClient`` the orchestrator builds talk to this transport."""
        real = ado.AdoClient
        #: The genuine class, for tests that need to patch a method on it.
        self.real_client_cls = real
        client = self.client

        def factory(**kwargs: Any) -> ado.AdoClient:
            return real(**kwargs, client=client)

        monkeypatch.setattr(ado, "AdoClient", factory)


@pytest.fixture
def fake_ado(bare_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAdo]:
    stub = FakeAdo(bare_repo)
    stub.install(monkeypatch)
    yield stub
    # AdoClient.aclose() deliberately leaves a caller-supplied client open, so close the
    # shared one here instead.
    asyncio.run(stub.client.aclose())


# --------------------------------------------------------------------------------------
# harnesses
# --------------------------------------------------------------------------------------


class ScriptedHarness:
    """A harness whose behaviour each test dictates.

    Implements the same protocol as the shipped harnesses (docs/architecture.md §4.2) so the
    orchestrator cannot tell the difference.
    """

    name = "scripted"

    def __init__(
        self,
        *,
        files: int = 1,
        exit_code: int = 0,
        summary: str = "Wrote one file.",
        stderr: str = "",
        raises: BaseException | None = None,
        side_effect: Callable[[Path], None] | None = None,
    ) -> None:
        self.files = files
        self.exit_code = exit_code
        self.summary = summary
        self.stderr = stderr
        self.raises = raises
        self.side_effect = side_effect
        self.calls: list[tuple[Path, str, int]] = []

    def preflight(self, cfg: Settings) -> None:
        return None

    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult:
        self.calls.append((repo_dir, prompt, timeout_s))
        if self.side_effect is not None:
            self.side_effect(repo_dir)
        if self.raises is not None:
            raise self.raises
        for n in range(self.files):
            (repo_dir / f"generated_{n}.txt").write_text(f"file {n}\n", encoding="utf-8")
        return HarnessResult(
            exit_code=self.exit_code,
            summary=self.summary,
            stdout=self.summary,
            stderr=self.stderr,
        )


@pytest.fixture
def fake_harness() -> FakeHarness:
    return FakeHarness()


# --------------------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------------------


def request_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": "1",
        "organization": ORG,
        "project": PROJECT,
        "workItem": {"type": "Bug", "id": 4123},
        "repository": REPO_NAME,
        "branch": TARGET_BRANCH,
        "baseBranch": DEFAULT_BRANCH,
        "correlationId": "test-correlation",
    }
    body.update(overrides)
    return body


def make_request(**overrides: Any) -> InvocationRequest:
    return InvocationRequest.model_validate(request_body(**overrides))
