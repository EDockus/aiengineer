"""Azure DevOps REST client: work items, repositories, refs and pull requests.

Auth is a **Personal Access Token** (``T2PR_ADO_PAT``), presented as HTTP ``Basic``. That
is a deliberate exception to an otherwise keyless design, not an oversight; the reasoning
and the routes that were tested before accepting it are the decision record in
docs/deploy.md §3:
Azure DevOps will not entitle a Foundry Entra agent identity (``VS403283``) and that
identity cannot federate into one Azure DevOps does accept (``AADSTS700231``). Foundry
*models* are still keyless -- ``DefaultAzureCredential``, see
:func:`ticket_to_pr.harness.foundry_token`. Only this file uses a PAT.

Do not "fix" this back to ``DefaultAzureCredential`` without re-testing those routes.

The whole of ADO auth is behind :class:`TokenProvider` so the swap is one class when the
platform catches up. Every HTTP failure is mapped to a v1 contract error code at the point
it happens.
"""

from __future__ import annotations

import base64
import html
import logging
import re
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from .config import AuthMode, Settings
from .models import ErrorCode, Repository, RunFailure, WorkItem
from .redact import register_secret

log = logging.getLogger(__name__)

API_VERSION = "7.1"
ZERO_OBJECT_ID = "0" * 40

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")

# Maximum linked work items whose titles we pull into the prompt. Bounded so a work
# item with hundreds of links cannot turn one invocation into hundreds of REST calls.
MAX_LINKED_ITEMS = 10


class TokenProvider(Protocol):
    """Supplies an ``Authorization`` header value for Azure DevOps.

    ``scheme`` is the auth scheme that goes in front of :meth:`token`'s value: ``Basic``
    for a PAT, ``Bearer`` for anything token-based. Keeping it on the provider means the
    REST client never has to know which one is in play.
    """

    scheme: str

    async def token(self) -> str: ...

    async def aclose(self) -> None: ...


class PatTokenProvider:
    """An Azure DevOps Personal Access Token, presented as HTTP Basic.

    Why a PAT and not the agent identity (docs/deploy.md §3): Azure DevOps refuses to
    entitle a Foundry Entra agent identity (``VS403283``) while accepting an ordinary
    service principal in the same organization, and that agent identity cannot federate
    into one Azure DevOps does accept (``AADSTS700231`` -- a token obtained through a
    federated credential may not be used as one). Foundry models still use the agent
    identity; only Azure DevOps uses this.

    Azure DevOps takes a PAT as the *password* of an HTTP Basic pair with an empty
    username. :meth:`token` therefore returns the base64 of ``":<pat>"``, and callers send
    it as ``Authorization: Basic ...`` -- see :attr:`scheme`.

    The value is held in memory only. It is never logged, never written to ``.git/config``
    (see :mod:`ticket_to_pr.git_ops`), never placed in argv, and never reaches a harness
    subprocess: ``T2PR_*`` is excluded from the harness environment allowlist, the harness
    runs as a different uid, and :mod:`ticket_to_pr.launch` keeps ``T2PR_ADO_PAT`` off the
    exec-time environment of every process in the container so ``/proc/<pid>/environ``
    cannot be read for it either.
    """

    #: Azure DevOps authenticates a PAT with Basic, not Bearer.
    scheme = "Basic"

    def __init__(self, pat: str) -> None:
        if not pat:
            raise RunFailure(
                ErrorCode.ADO_FORBIDDEN,
                "no Azure DevOps Personal Access Token is configured (T2PR_ADO_PAT)",
            )
        self._encoded = base64.b64encode(f":{pat}".encode()).decode()
        # The raw PAT is registered at startup (main.create_app). Register the encoded form
        # too: this is the string that actually travels in an Authorization header, so it
        # is the one that would surface if a client library ever echoed a request back into
        # an exception message or a debug log. Redaction is by value, so both forms need it.
        register_secret(self._encoded)

    async def token(self) -> str:
        return self._encoded

    async def aclose(self) -> None:
        return None


class StaticTokenProvider:
    """Dev-mode only: a fixed, meaningless token handed to a loopback stub.

    Reachable only when :func:`ticket_to_pr.config.Settings.from_env` has already proved
    the process is on a laptop talking to 127.0.0.1.
    """

    #: The dev stub accepts anything; Bearer keeps the local traffic shaped like the real
    #: thing for anyone reading it.
    scheme = "Bearer"

    def __init__(self, value: str) -> None:
        self._value = value

    async def token(self) -> str:
        return self._value

    async def aclose(self) -> None:
        return None


def build_token_provider(
    cfg: Settings, dev_placeholder: str = "dev-local-stub-value"
) -> TokenProvider:
    if cfg.auth_mode is AuthMode.DEV_STATIC:
        log.warning(
            "dev mode: using a static Azure DevOps token against %s", cfg.ado_base_url
        )
        return StaticTokenProvider(dev_placeholder)
    log.info("Azure DevOps auth: Personal Access Token (docs/deploy.md section 3)")
    return PatTokenProvider(cfg.ado_pat)


class AdoClient:
    """Thin async REST client scoped to one organization."""

    def __init__(
        self,
        *,
        cfg: Settings,
        organization: str,
        tokens: TokenProvider,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._org_url = cfg.org_url(organization)
        self._tokens = tokens
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        not_found: ErrorCode = ErrorCode.INTERNAL,
        what: str = "resource",
    ) -> Any:
        url = f"{self._org_url}/{path.lstrip('/')}"
        query = {"api-version": API_VERSION, **(params or {})}
        headers = {
            "Authorization": f"{self._tokens.scheme} {await self._tokens.token()}",
            "Accept": "application/json",
        }
        try:
            response = await self._client.request(
                method, url, params=query, json=json_body, headers=headers
            )
        except httpx.HTTPError as exc:
            # Never include the exception's request headers in the message. Ambiguous: a
            # timeout on the response says nothing about whether the server acted.
            raise RunFailure(
                ErrorCode.INTERNAL,
                f"Azure DevOps request for {what} failed: {type(exc).__name__}",
                ambiguous=True,
            ) from exc

        if response.status_code in (401, 403):
            # Named in the order they actually happen. Azure DevOps auth is a PAT
            # (docs/deploy.md §3), and a PAT's commonest failure by a wide margin is
            # that it expired -- Azure DevOps answers an expired or revoked PAT with the
            # same 401/403 it uses for a scope it will not grant, so the message has to
            # offer both. Blaming the agent identity, which is not what authenticated this
            # request, sent the reader looking in the wrong place entirely.
            raise RunFailure(
                ErrorCode.ADO_FORBIDDEN,
                f"Azure DevOps refused access to {what} (HTTP {response.status_code}). "
                "The Personal Access Token in T2PR_ADO_PAT has most likely expired or "
                "been revoked; otherwise it is missing a scope (Work Items Read, Code "
                "Read & Write, Pull Request Read & Write) or is scoped to a different "
                "organization",
            )
        if response.status_code == 404:
            raise RunFailure(not_found, f"{what} was not found")
        if response.status_code >= 400:
            raise RunFailure(
                ErrorCode.INTERNAL,
                f"Azure DevOps returned HTTP {response.status_code} for {what}",
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            # Azure DevOps answers an unauthenticated request with a 203 sign-in page
            # rather than a 401. Treat any non-JSON body as an auth failure.
            raise RunFailure(
                ErrorCode.ADO_FORBIDDEN,
                f"Azure DevOps returned a non-JSON response for {what}; the agent "
                "identity is probably not a member of the organization",
            )
        return response.json()

    # ---------------------------------------------------------------- work items

    async def list_work_item_types(self, project: str) -> frozenset[str]:
        data = await self._request(
            "GET",
            f"{project}/_apis/wit/workitemtypes",
            not_found=ErrorCode.WORK_ITEM_NOT_FOUND,
            what=f"work item types for project {project!r}",
        )
        return frozenset(str(item["name"]) for item in data.get("value", []))

    async def get_work_item(self, project: str, work_item_id: int) -> WorkItem:
        data = await self._request(
            "GET",
            f"{project}/_apis/wit/workitems/{work_item_id}",
            params={"$expand": "relations"},
            not_found=ErrorCode.WORK_ITEM_NOT_FOUND,
            what=f"work item #{work_item_id}",
        )
        fields: dict[str, Any] = data.get("fields", {})
        linked = await self._linked_titles(project, data.get("relations") or [])
        tags = tuple(
            t.strip() for t in str(fields.get("System.Tags", "")).split(";") if t.strip()
        )
        return WorkItem(
            id=work_item_id,
            type=str(fields.get("System.WorkItemType", "")),
            title=str(fields.get("System.Title", "")),
            description=strip_html(fields.get("System.Description")),
            acceptance_criteria=strip_html(
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            ),
            repro_steps=strip_html(fields.get("Microsoft.VSTS.TCM.ReproSteps")),
            tags=tags,
            linked_titles=linked,
        )

    async def _linked_titles(self, project: str, relations: list[Any]) -> tuple[str, ...]:
        ids: list[int] = []
        for rel in relations:
            if not str(rel.get("rel", "")).startswith("System.LinkTypes"):
                continue
            match = re.search(r"/workItems/(\d+)$", str(rel.get("url", "")))
            if match:
                ids.append(int(match.group(1)))
        titles: list[str] = []
        for linked_id in ids[:MAX_LINKED_ITEMS]:
            try:
                data = await self._request(
                    "GET",
                    f"{project}/_apis/wit/workitems/{linked_id}",
                    params={"fields": "System.Title"},
                    not_found=ErrorCode.WORK_ITEM_NOT_FOUND,
                    what=f"linked work item #{linked_id}",
                )
            except RunFailure:
                # A link the agent cannot read is context we do without, not a run failure.
                continue
            title = str(data.get("fields", {}).get("System.Title", "")).strip()
            if title:
                titles.append(f"#{linked_id} {title}")
        return tuple(titles)

    # ---------------------------------------------------------------- repositories

    async def get_repository(self, project: str, repository: str) -> Repository:
        data = await self._request(
            "GET",
            f"{project}/_apis/git/repositories/{repository}",
            not_found=ErrorCode.REPOSITORY_NOT_FOUND,
            what=f"repository {repository!r}",
        )
        default_branch = str(data.get("defaultBranch") or "refs/heads/main")
        return Repository(
            id=str(data["id"]),
            name=str(data["name"]),
            remote_url=str(data.get("remoteUrl") or data.get("sshUrl") or ""),
            default_branch=default_branch.removeprefix("refs/heads/"),
            web_url=str(data.get("webUrl") or ""),
        )

    async def get_branch_object_id(self, project: str, repo_id: str, branch: str) -> str | None:
        """Return the tip commit of ``branch``, or ``None`` if the branch does not exist."""
        data = await self._request(
            "GET",
            f"{project}/_apis/git/repositories/{repo_id}/refs",
            params={"filter": f"heads/{branch}"},
            not_found=ErrorCode.REPOSITORY_NOT_FOUND,
            what=f"refs of repository {repo_id!r}",
        )
        wanted = f"refs/heads/{branch}"
        for ref in data.get("value", []):
            if str(ref.get("name")) == wanted:
                return str(ref["objectId"])
        return None

    async def delete_branch(
        self, project: str, repo_id: str, branch: str, object_id: str
    ) -> None:
        """Best-effort remote branch deletion, used to avoid dangling branches."""
        await self._request(
            "POST",
            f"{project}/_apis/git/repositories/{repo_id}/refs",
            json_body=[
                {
                    "name": f"refs/heads/{branch}",
                    "oldObjectId": object_id,
                    "newObjectId": ZERO_OBJECT_ID,
                }
            ],
            what=f"deletion of branch {branch!r}",
        )

    # ---------------------------------------------------------------- pull requests

    async def create_pull_request(
        self,
        *,
        project: str,
        repo: Repository,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool,
        reviewers: tuple[str, ...],
    ) -> tuple[int, str]:
        body: dict[str, Any] = {
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
            "isDraft": draft,
        }
        if reviewers:
            body["reviewers"] = [{"id": r} for r in reviewers]
        try:
            data = await self._request(
                "POST",
                f"{project}/_apis/git/repositories/{repo.id}/pullrequests",
                json_body=body,
                not_found=ErrorCode.PR_FAILED,
                what="pull request creation",
            )
        except RunFailure as exc:
            # docs/architecture.md §3 step 9: every way this call can fail is `pr_failed`, not
            # 404. Azure DevOps answers 409 when a pull request already exists for the two
            # branches and 400 for an unusable source or target ref -- the likeliest real
            # failures. The branch is on the remote by now, so reporting `internal` would
            # tell the caller nothing was pushed, which is untrue. A permission failure
            # keeps its own code.
            if exc.code is ErrorCode.ADO_FORBIDDEN:
                raise
            raise RunFailure(ErrorCode.PR_FAILED, exc.message, ambiguous=exc.ambiguous) from exc
        pr_id = int(data["pullRequestId"])
        web_url = str(data.get("_links", {}).get("web", {}).get("href") or "")
        if not web_url:
            base = repo.web_url or f"{self._org_url}/{project}/_git/{repo.name}"
            web_url = f"{base}/pullrequest/{pr_id}"
        return pr_id, web_url

    async def find_pull_request(
        self, project: str, repo_id: str, source_branch: str
    ) -> int | None:
        """Id of any pull request already open on ``source_branch``, else ``None``.

        Asked before the post-push cleanup deletes a branch: pull request creation can
        fail on this side of the wire *after* Azure DevOps created one -- a read timeout is
        enough -- and deleting the source branch of a live pull request would be a worse
        outcome than the dangling branch the cleanup exists to prevent. Raises
        :class:`~ticket_to_pr.models.RunFailure` if the question cannot be answered; the
        caller treats that as "maybe" and leaves the branch alone.
        """
        data = await self._request(
            "GET",
            f"{project}/_apis/git/repositories/{repo_id}/pullrequests",
            params={
                "searchCriteria.sourceRefName": f"refs/heads/{source_branch}",
                "searchCriteria.status": "all",
                "$top": "1",
            },
            not_found=ErrorCode.PR_FAILED,
            what="pull request lookup",
        )
        values = data.get("value") if isinstance(data, dict) else None
        if isinstance(values, list) and values:
            return int(values[0]["pullRequestId"])
        return None


def strip_html(value: Any) -> str:
    """Flatten an ADO rich-text field to plain text (docs/architecture.md §4.3).

    The result is **plain text, not HTML**, and entity references are resolved last so the
    text says what the field said: a description containing the literal characters
    ``<script>`` (stored as ``&lt;script&gt;``) comes back as ``<script>``, because that is
    what the author typed. Unescaping first would instead delete it, which is a lossy and
    equally wrong answer -- ``&lt;d&gt;`` is prose, not markup.

    Any caller that renders this into HTML must escape it there. The one such caller in
    this repository, ``examples/function-app``, calls :func:`html.escape` on every value it
    interpolates into a work-item comment.
    """
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
