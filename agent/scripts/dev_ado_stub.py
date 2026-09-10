#!/usr/bin/env python3
"""A local stand-in for Azure DevOps, for running the agent with no Azure tenant.

This is developer tooling, not part of the shipped service. It serves the handful of
Azure DevOps REST endpoints ``ticket_to_pr.ado`` actually calls, and backs the git side
with a *real* bare repository on disk that the agent clones from and pushes to over
``file://``. That matters: the point is to exercise the real ``ado.py`` and ``git_ops.py``
code paths end to end rather than to mock them out.

It binds to loopback only and refuses to start otherwise.

    uv run --with fastapi --with uvicorn python scripts/dev_ado_stub.py --reset

Endpoints served (all under ``/{org}/{project}/_apis/...``, api-version ignored):
    GET  wit/workitemtypes
    GET  wit/workitems/{id}
    GET  git/repositories/{repo}
    GET  git/repositories/{repo}/refs
    POST git/repositories/{repo}/refs          (ref update / delete)
    POST git/repositories/{repo}/pullrequests
    GET  /_dev/pullrequests                    (inspection, not an ADO endpoint)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

ORG = "contoso"
PROJECT = "Platform"
REPO_NAME = "payments-api"
REPO_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_BRANCH = "main"
ZERO = "0" * 40

WORK_ITEM_TYPES = ["Bug", "Task", "User Story", "Feature", "Epic"]

#: Work item 4123 is the happy path; 9999 deliberately 404s so the
#: ``work_item_not_found`` branch can be exercised.
MISSING_WORK_ITEM_ID = 9999


def git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def seed_repo(bare: Path) -> None:
    """Create a bare repo with one commit on ``main``, as a real remote to clone."""
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "--initial-branch", DEFAULT_BRANCH, str(bare)])
    with tempfile.TemporaryDirectory(prefix="t2pr-seed-") as scratch:
        work = Path(scratch) / "work"
        git(["clone", f"file://{bare}", str(work)])
        (work / "README.md").write_text(
            "# payments-api\n\nA fake repository served by the local Azure DevOps stub.\n",
            encoding="utf-8",
        )
        (work / "app.py").write_text(
            'def charge(amount: int) -> int:\n    """Charge an amount in minor units."""\n'
            "    return amount\n",
            encoding="utf-8",
        )
        git(["add", "-A"], cwd=work)
        git(["-c", "user.name=Dev Stub", "-c", "user.email=dev@local",
             "commit", "-m", "Initial commit"], cwd=work)
        git(["push", "origin", DEFAULT_BRANCH], cwd=work)
    # Allow the agent to push into this bare repo from a shallow clone.
    git(["config", "uploadpack.allowFilter", "true"], cwd=bare)
    git(["config", "receive.denyDeletes", "false"], cwd=bare)


def build_app(bare: Path, base_url: str) -> FastAPI:
    app = FastAPI(title="dev-ado-stub", docs_url=None, redoc_url=None)
    pull_requests: list[dict[str, Any]] = []

    def require_auth(authorization: str | None) -> None:
        """Prove the agent really sends a bearer token on every call."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")

    def check_scope(org: str, project: str) -> None:
        if org != ORG or project != PROJECT:
            raise HTTPException(status_code=404, detail="unknown org or project")

    def refs() -> dict[str, str]:
        out = git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], cwd=bare)
        result: dict[str, str] = {}
        for line in out.splitlines():
            name, _, sha = line.partition(" ")
            if name:
                result[name] = sha.strip()
        return result

    @app.get("/{org}/{project}/_apis/wit/workitemtypes")
    async def work_item_types(
        org: str, project: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_auth(authorization)
        check_scope(org, project)
        return {
            "count": len(WORK_ITEM_TYPES),
            "value": [{"name": name} for name in WORK_ITEM_TYPES],
        }

    @app.get("/{org}/{project}/_apis/wit/workitems/{work_item_id}")
    async def work_item(
        org: str,
        project: str,
        work_item_id: int,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_auth(authorization)
        check_scope(org, project)
        if work_item_id == MISSING_WORK_ITEM_ID:
            raise HTTPException(status_code=404, detail="work item does not exist")
        return {
            "id": work_item_id,
            "fields": {
                "System.WorkItemType": "Bug",
                "System.Title": "Null reference when charging a zero-amount order",
                "System.Description": (
                    "<div><p>Calling <code>charge()</code> with a zero amount raises a "
                    "<b>NullReferenceException</b> in production.</p></div>"
                ),
                "Microsoft.VSTS.Common.AcceptanceCriteria": (
                    "<ul><li>charge(0) returns 0</li><li>a regression test covers it</li></ul>"
                ),
                "Microsoft.VSTS.TCM.ReproSteps": (
                    "<ol><li>POST /charge with amount=0</li><li>Observe the 500</li></ol>"
                ),
                "System.Tags": "agent; payments",
            },
            "relations": [],
        }

    @app.get("/{org}/{project}/_apis/git/repositories/{repository}")
    async def repository(
        org: str,
        project: str,
        repository: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_auth(authorization)
        check_scope(org, project)
        if repository not in (REPO_NAME, REPO_ID):
            raise HTTPException(status_code=404, detail="unknown repository")
        return {
            "id": REPO_ID,
            "name": REPO_NAME,
            # A real file:// remote, so git_ops.clone/push do genuine work.
            "remoteUrl": f"file://{bare}",
            "defaultBranch": f"refs/heads/{DEFAULT_BRANCH}",
            "webUrl": f"{base_url}/{ORG}/{PROJECT}/_git/{REPO_NAME}",
        }

    @app.get("/{org}/{project}/_apis/git/repositories/{repository}/refs")
    async def list_refs(
        org: str,
        project: str,
        repository: str,
        ref_filter: str = Query(default="", alias="filter"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_auth(authorization)
        check_scope(org, project)
        wanted = f"refs/{ref_filter}" if ref_filter else "refs/heads/"
        value = [
            {"name": name, "objectId": sha}
            for name, sha in refs().items()
            if name == wanted or name.startswith(wanted)
        ]
        return {"count": len(value), "value": value}

    @app.post("/{org}/{project}/_apis/git/repositories/{repository}/refs")
    async def update_refs(
        org: str,
        project: str,
        repository: str,
        body: list[dict[str, Any]],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_auth(authorization)
        check_scope(org, project)
        results = []
        for item in body:
            name = str(item.get("name", ""))
            new_object_id = str(item.get("newObjectId", ""))
            if new_object_id == ZERO:
                git(["update-ref", "-d", name], cwd=bare)
                results.append({"name": name, "success": True, "updateStatus": "succeeded"})
            else:
                git(["update-ref", name, new_object_id], cwd=bare)
                results.append({"name": name, "success": True, "updateStatus": "succeeded"})
        return {"count": len(results), "value": results}

    @app.post("/{org}/{project}/_apis/git/repositories/{repository}/pullrequests")
    async def create_pr(
        org: str,
        project: str,
        repository: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        require_auth(authorization)
        check_scope(org, project)
        source = str(body.get("sourceRefName", ""))
        if source not in refs():
            raise HTTPException(status_code=400, detail=f"source ref {source} was never pushed")
        pr_id = 800 + len(pull_requests) + 1
        web = f"{base_url}/{ORG}/{PROJECT}/_git/{REPO_NAME}/pullrequest/{pr_id}"
        record = {
            "pullRequestId": pr_id,
            "title": body.get("title"),
            "description": body.get("description"),
            "sourceRefName": source,
            "targetRefName": body.get("targetRefName"),
            "isDraft": body.get("isDraft"),
            "reviewers": body.get("reviewers", []),
            "_links": {"web": {"href": web}},
        }
        pull_requests.append(record)
        return JSONResponse(status_code=201, content=record)

    @app.get("/_dev/pullrequests")
    async def dev_pull_requests() -> dict[str, Any]:
        """Not an Azure DevOps endpoint. Lets you see what the agent created."""
        return {
            "bareRepo": str(bare),
            "branches": refs(),
            "count": len(pull_requests),
            "value": pull_requests,
        }

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent.parent / ".dev-ado"),
        help="where the fake bare repository lives",
    )
    parser.add_argument("--reset", action="store_true", help="recreate the bare repository")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"refusing to bind {args.host}: this stub has no authentication and is "
            "loopback-only by design.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    data_dir = Path(args.data_dir).resolve()
    bare = data_dir / f"{REPO_NAME}.git"
    if args.reset and data_dir.exists():
        shutil.rmtree(data_dir)
    if not bare.exists():
        seed_repo(bare)
        print(f"seeded bare repository at {bare}", flush=True)

    base_url = f"http://{args.host}:{args.port}"
    print(
        f"dev ADO stub on {base_url}  (org={ORG} project={PROJECT} repo={REPO_NAME})",
        flush=True,
    )
    uvicorn.run(build_app(bare, base_url), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
