"""Sample Azure DevOps service-hook receiver for the ticket-to-pr agent.

See this directory's README for deploying it and configuring the service hook, and
docs/contract.md for the request and response it speaks.

Two functions:

``service_hook``
    HTTP trigger. Receives an Azure DevOps ``workitem.created`` (or ``workitem.updated``)
    service hook, ignores anything that is not tagged with :data:`OPT_IN_TAG`, builds the
    v1 contract body, POSTs it to the agent, records the run in table storage and writes
    the run id back to the work item as a comment.

``poll_runs``
    Timer trigger. Polls every pending run's status and, when it finishes, posts the pull
    request link (or the failure) back to the work item as a second comment.

Everything authenticates with the Function App's managed identity:

* the agent, with the Foundry data-plane scope :data:`AGENT_SCOPE`;
* Azure DevOps, with the fixed ADO resource scope :data:`ADO_SCOPE` (docs/deploy.md §3);
* table storage, with the storage data-plane scope.

There are no keys, no PATs and no connection strings with secrets in them. See README.md
for the app settings and the role assignments each of those needs.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import os
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit

import azure.functions as func
import requests
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential

log = logging.getLogger(__name__)

app = func.FunctionApp()

#: Only work items carrying this tag are sent to the agent. This is the opt-in: without
#: it a service hook on "work item created" would fire the agent on every new ticket.
OPT_IN_TAG: Final = os.environ.get("T2PR_OPT_IN_TAG", "agent").casefold()

#: Entra scope for the Foundry data plane, which is what the agent endpoint authenticates
#: against. Verified against learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent.
AGENT_SCOPE: Final = "https://ai.azure.com/.default"

#: Azure DevOps' fixed resource id (docs/deploy.md §3). Never a PAT.
ADO_SCOPE: Final = "499b84ac-1321-427f-aa17-267ca6975798/.default"

#: The work item *comments* endpoint is only exposed on a preview api-version.
ADO_COMMENTS_API_VERSION: Final = "7.1-preview.4"

#: Foundry gateway api-version for the invocations protocol.
AGENT_API_VERSION: Final = "v1"

HTTP_TIMEOUT: Final = 30

#: Field that may carry the repository name, if you added one to your process template.
REPO_FIELD: Final = os.environ.get("T2PR_REPO_FIELD", "Custom.Repository")

#: Fallback: a ``repo:<name>`` tag on the work item.
REPO_TAG_RE: Final = re.compile(r"^repo:(?P<name>[A-Za-z0-9][A-Za-z0-9 ._-]{0,63})$", re.I)

#: Azure DevOps organization and project names, matching the agent's own ``models.NAME_RE``.
#: Both values arrive in the service-hook body and both are interpolated into REST URLs, so
#: anything path-ish or scheme-ish is rejected here rather than trusted to be encoded later.
NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

_credential: TokenCredential | None = None
_table_client: TableClient | None = None


def _cred() -> TokenCredential:
    """One process-wide credential so token caching actually happens."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _token(scope: str) -> str:
    token: AccessToken = _cred().get_token(scope)
    return token.token


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"app setting {name} is required but is not set")
    return value


# --------------------------------------------------------------------------------------
# run state
# --------------------------------------------------------------------------------------


def _table() -> TableClient:
    """Table that tracks in-flight runs so the poller can find them again.

    Identity-based: ``T2PR_STATE_ACCOUNT_URL`` is a plain table endpoint such as
    ``https://mystorage.table.core.windows.net``. No account key anywhere.
    """
    global _table_client
    if _table_client is None:
        client = TableClient(
            endpoint=_setting("T2PR_STATE_ACCOUNT_URL"),
            table_name=os.environ.get("T2PR_STATE_TABLE", "ticketToPrRuns"),
            credential=_cred(),
        )
        with contextlib.suppress(ResourceExistsError):
            client.create_table()
        _table_client = client
    return _table_client


# --------------------------------------------------------------------------------------
# Azure DevOps helpers
# --------------------------------------------------------------------------------------


def _check_name(value: str, what: str) -> str:
    """Reject an organization or project name that is not one.

    Both come out of the request body. Without this an attacker who can reach the function
    could set ``System.TeamProject`` to ``MyProj/_apis/...?x=`` and steer the request --
    carrying the Function App's managed-identity token -- at a different Azure DevOps
    endpoint entirely; ``requests`` does not encode ``/`` or ``?`` in a URL it is handed.
    """
    value = value.strip()
    if not NAME_RE.match(value):
        raise ValueError(f"{what} {value!r} is not a valid Azure DevOps name")
    return value


def _ado_comment(organization: str, project: str, work_item_id: int, text: str) -> None:
    """Add a comment to a work item. Best effort: a failed comment must not fail the run."""
    try:
        url = (
            f"https://dev.azure.com/{quote(_check_name(organization, 'organization'), safe='')}"
            f"/{quote(_check_name(project, 'project'), safe='')}"
            f"/_apis/wit/workItems/{int(work_item_id)}/comments"
        )
    except (TypeError, ValueError) as exc:
        log.warning("refusing to build a comment URL: %s", exc)
        return
    response = requests.post(
        url,
        params={"api-version": ADO_COMMENTS_API_VERSION},
        headers={
            "Authorization": f"Bearer {_token(ADO_SCOPE)}",
            "Content-Type": "application/json",
        },
        json={"text": text},
        timeout=HTTP_TIMEOUT,
    )
    if not response.ok:
        # Never log the response body wholesale; it can echo work-item content.
        log.warning(
            "could not comment on %s/%s#%s: HTTP %s",
            organization,
            project,
            work_item_id,
            response.status_code,
        )


# --------------------------------------------------------------------------------------
# payload parsing
# --------------------------------------------------------------------------------------


def _esc(value: Any) -> str:
    """HTML-escape a value on its way into a work-item comment, quotes included."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _href(value: Any) -> str:
    """An escaped ``href``, or empty if the URL is not plainly an http(s) one.

    Escaping stops a value breaking out of the attribute; this stops a ``javascript:`` or
    ``data:`` URL being a live link inside it.
    """
    url = str(value or "")
    return _esc(url) if url.lower().startswith(("https://", "http://")) else ""


def _slug(value: str, *, max_length: int = 40) -> str:
    """ASCII, lower case, hyphen separated -- safe for the branch name regex."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").casefold()
    return slug[:max_length].strip("-") or "work-item"


def _organization(resource: dict[str, Any]) -> str:
    """Organization name from the payload's own work item URL.

    The service hook body does not carry the org *name* as a field -- only ids in
    ``resourceContainers`` -- so it is taken from ``resource.url``, which is
    ``https://dev.azure.com/{org}/_apis/wit/workItems/{id}``. ``T2PR_ADO_ORGANIZATION``
    overrides it for on-premises or ``*.visualstudio.com`` hosts.
    """
    override = os.environ.get("T2PR_ADO_ORGANIZATION", "").strip()
    if override:
        return _check_name(override, "organization")
    url = str(resource.get("url", ""))
    path = urlsplit(url).path.strip("/")
    first = path.split("/", 1)[0] if path else ""
    if not first:
        raise ValueError("could not determine the organization from the service hook payload")
    return _check_name(first, "organization")


def _tags(fields: dict[str, Any]) -> list[str]:
    raw = str(fields.get("System.Tags", ""))
    return [tag.strip() for tag in raw.split(";") if tag.strip()]


def _repository(fields: dict[str, Any], tags: list[str]) -> str:
    """Repository name: the custom field first, then a ``repo:<name>`` tag."""
    from_field = str(fields.get(REPO_FIELD, "")).strip()
    if from_field:
        return from_field
    for tag in tags:
        match = REPO_TAG_RE.match(tag)
        if match:
            return match.group("name")
    raise ValueError(
        f"no repository: set the {REPO_FIELD} field or add a 'repo:<name>' tag to the work item"
    )


def _contract_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build the v1 request body, or ``None`` when the work item has not opted in.

    Note what is *not* here: no ``harness``, no ``model``, no ``coder``. Those are
    deploy-time configuration on the agent and the contract rejects them with 400
    (docs/contract.md §3).
    """
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        raise ValueError("service hook payload has no 'resource' object")
    fields = resource.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("service hook payload has no 'resource.fields' object")

    tags = _tags(fields)
    if OPT_IN_TAG not in {tag.casefold() for tag in tags}:
        return None

    work_item_id = int(resource.get("id") or fields.get("System.Id") or 0)
    if work_item_id <= 0:
        raise ValueError("service hook payload has no work item id")

    work_item_type = str(fields.get("System.WorkItemType", "")).strip()
    title = str(fields.get("System.Title", "")).strip()
    project = str(fields.get("System.TeamProject", "")).strip()
    if not (work_item_type and title and project):
        raise ValueError("service hook payload is missing type, title or team project")
    project = _check_name(project, "project")

    body: dict[str, Any] = {
        "version": "1",
        "organization": _organization(resource),
        "project": project,
        "workItem": {"type": work_item_type, "id": work_item_id},
        "repository": _repository(fields, tags),
        "branch": f"{_slug(work_item_type, max_length=16)}/{work_item_id}-{_slug(title)}",
        "correlationId": f"service-hook-{work_item_id}",
    }
    base_branch = str(fields.get("Custom.BaseBranch", "")).strip()
    if base_branch:
        body["baseBranch"] = base_branch
    return body


# --------------------------------------------------------------------------------------
# agent calls
# --------------------------------------------------------------------------------------


def _agent_url(suffix: str = "") -> str:
    """``{project endpoint}/agents/{name}/endpoint/protocols/invocations`` plus a suffix.

    ``T2PR_AGENT_ENDPOINT`` is the full invocations URL, which ``azd`` writes to the
    environment as ``AGENT_<SERVICE>_INVOCATIONS_ENDPOINT`` after a deploy.
    """
    return _setting("T2PR_AGENT_ENDPOINT").rstrip("/") + suffix


def _start_run(body: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        _agent_url(),
        params={"api-version": AGENT_API_VERSION},
        headers={
            "Authorization": f"Bearer {_token(AGENT_SCOPE)}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def _run_status(run_id: str) -> dict[str, Any]:
    response = requests.get(
        _agent_url(f"/{run_id}"),
        params={"api-version": AGENT_API_VERSION},
        headers={"Authorization": f"Bearer {_token(AGENT_SCOPE)}"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


# --------------------------------------------------------------------------------------
# functions
# --------------------------------------------------------------------------------------


@app.route(route="service-hook", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def service_hook(req: func.HttpRequest) -> func.HttpResponse:
    """Receive an Azure DevOps work item service hook and start an agent run."""
    try:
        payload: dict[str, Any] = req.get_json()
    except ValueError:
        return func.HttpResponse("expected a JSON body", status_code=400)

    try:
        body = _contract_body(payload)
    except ValueError as exc:
        # A malformed or unusable payload is the caller's problem, not a retryable one:
        # 400 so Azure DevOps stops redelivering it.
        log.warning("rejecting service hook: %s", exc)
        return func.HttpResponse(str(exc), status_code=400)

    if body is None:
        return func.HttpResponse(
            f"ignored: work item is not tagged '{OPT_IN_TAG}'", status_code=200
        )

    work_item = body["workItem"]
    try:
        accepted = _start_run(body)
    except requests.RequestException as exc:
        log.error("agent rejected the run for work item %s: %s", work_item["id"], exc)
        _ado_comment(
            body["organization"],
            body["project"],
            int(work_item["id"]),
            "**Ticket-to-PR agent** could not start a run. See the Function App logs.",
        )
        return func.HttpResponse("agent call failed", status_code=502)

    run_id = str(accepted.get("runId", ""))
    _table().upsert_entity(
        {
            "PartitionKey": body["organization"],
            "RowKey": run_id,
            "project": body["project"],
            "workItemId": int(work_item["id"]),
            "branch": body["branch"],
            "startedAt": datetime.now(UTC).isoformat(),
        },
        mode=UpdateMode.REPLACE,
    )
    _ado_comment(
        body["organization"],
        body["project"],
        int(work_item["id"]),
        f"**Ticket-to-PR agent** started run <code>{_esc(run_id)}</code> "
        f"on branch <code>{_esc(body['branch'])}</code>.",
    )
    log.info("started run %s for work item %s", run_id, work_item["id"])
    return func.HttpResponse(
        json.dumps({"runId": run_id}), status_code=202, mimetype="application/json"
    )


@app.timer_trigger(schedule="0 */1 * * * *", arg_name="timer", run_on_startup=False)
def poll_runs(timer: func.TimerRequest) -> None:
    """Every minute, finish off any run that has completed since the last tick."""
    table = _table()
    for entity in table.list_entities():
        organization = str(entity["PartitionKey"])
        run_id = str(entity["RowKey"])
        project = str(entity["project"])
        work_item_id = int(entity["workItemId"])

        try:
            status = _run_status(run_id)
        except requests.RequestException as exc:
            log.warning("could not poll run %s: %s", run_id, exc)
            continue

        state = str(status.get("status", ""))
        if state in {"queued", "running"}:
            continue

        # Azure DevOps renders work-item comments as HTML. Every value below came back
        # over the wire and some of it (an error message carrying harness stderr) is
        # shaped by whoever wrote the work item, so all of it is escaped. ADO strips
        # <script>, but it keeps anchors -- and an unescaped
        # <a href="https://evil.example">Approved by the security team</a> in a ticket
        # everyone reads is the whole attack.
        if state == "succeeded":
            result = status.get("result") or {}
            comment = (
                "**Ticket-to-PR agent** opened "
                f'<a href="{_href(result.get("pullRequestUrl"))}">pull request '
                f"#{_esc(result.get('pullRequestId', '?'))}</a> from branch "
                f"<code>{_esc(result.get('branch'))}</code> "
                f"({_esc(result.get('filesChanged', 0))} files changed)."
            )
        else:
            error = status.get("error") or {}
            comment = (
                "**Ticket-to-PR agent** run failed: "
                f"<code>{_esc(error.get('code', 'internal'))}</code> - "
                f"{_esc(error.get('message', 'no message'))}"
            )

        _ado_comment(organization, project, work_item_id, comment)
        table.delete_entity(partition_key=organization, row_key=run_id)
        log.info("run %s finished with status %s", run_id, state)
