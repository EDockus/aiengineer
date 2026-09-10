# Request/response contract — v1

This is the **published interface**. Anything that can get an Entra token for the agent
can drive it: the ADO extension in this repository, the
[sample Function App](../examples/function-app/), or your own code.

Breaking changes bump the major version and the path. Additive, optional fields do not.

---

## 1. Endpoints

The agent container serves four routes:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/invocations` | Start a run. Returns immediately with a run id. |
| `GET` | `/invocations/{runId}` | Poll a run's status. |
| `GET` | `/healthz` | Liveness. `200` once configuration validated at startup. |
| `GET` | `/readiness` | Platform health probe, required by the Foundry hosted-agent runtime contract. Identical to `/healthz`. A non-200 causes the platform to restart the instance. |

Deployed on Foundry, callers reach the agent through the project's agent endpoint. The
gateway forwards to the container's `/invocations`:

```
https://{account}.services.ai.azure.com/api/projects/{project}/agents/ticket-to-pr/endpoint/protocols/invocations?api-version=v1
```

After `azd up`, `azd` writes that URL into the active environment as
`AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT`; `azd ai agent show` prints it too.

Coding runs take minutes, so the agent is **asynchronous by design**: `POST` returns
`202` with a run id and the caller polls. The Invocations protocol does not prescribe a
polling contract — the platform passes your payload through untouched and leaves the
status endpoint to you — so `GET /invocations/{runId}` is this agent's own contract, and
that is what the shapes below define.

## 2. Authentication

`Authorization: Bearer <Entra token for the Foundry data plane>`

```bash
TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)
```

The caller's identity — user, service principal or managed identity — needs the
**Foundry Agent Consumer** role (previously named `Azure AI User`, which also works) on
the Foundry project or on the specific agent. See
[deploy.md → Callers](deploy.md#5-let-callers-invoke-the-agent).

The gateway never forwards the `Authorization` header to the container. The agent
authorizes on the *organization allowlist* (`T2PR_ALLOWED_ORGS`), not on the caller.

---

## 3. Request

```
POST {agent-invocations-url}
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "version": "1",
  "organization": "contoso",
  "project": "Platform",
  "workItem": { "type": "Bug", "id": 4123 },
  "repository": "payments-api",
  "branch": "bugfix/4123-null-ref",
  "baseBranch": "main",
  "correlationId": "optional-caller-supplied-string"
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `version` | string | yes | Must be exactly `"1"`. |
| `organization` | string | yes | Azure DevOps organization **name**, not a URL. 1–64 chars, `^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$`. Must be in `T2PR_ALLOWED_ORGS`. |
| `project` | string | yes | Project name. Same character rules. |
| `workItem.type` | string | yes | 1–128 chars. Validated against the work item types the project actually returns. |
| `workItem.id` | integer | yes | Positive. |
| `repository` | string | yes | Repository name within the project. Same character rules as `organization`. |
| `branch` | string | yes | New branch name, 1–200 chars, must match `^[A-Za-z0-9._/-]+$`. Must not already exist. |
| `baseBranch` | string | no | Branch to fork from. Same regex. Defaults to the repository's default branch. |
| `correlationId` | string | no | Echoed back unchanged. Max 200 chars. |

### There is no `harness`, `model` or `coder` field

Harness and model are deploy-time configuration on the agent (see
[configuration.md](configuration.md)). Callers cannot choose them, and **unknown fields
are rejected with `400`** rather than ignored:

```json
{"error":{"code":"bad_request","details":[{"field":"harness","reason":"Extra inputs are not permitted"}]}}
```

Every `400` uses that envelope: `error.code` is always `bad_request`, and `error.details`
names each offending field once. The submitted *value* is never echoed back, so a rejected
branch name does not reappear in your logs.

Note also that the wire format is camelCase *only*. `work_item` is an unknown field and
gets a `400` just like `harness` does.

### Extra branch rules

Beyond the published regex, the agent rejects branch names that git itself would reject
or that would be ambiguous on a command line: leading `-` or `/`, trailing `/`, `.` or
`.lock`, and any occurrence of `..`, `//` or `@{`. These are `400`s.

---

## 4. Immediate response — `202 Accepted`

```json
{
  "runId": "690cd3bc-4098-4dff-a94a-e275088f3738",
  "status": "queued",
  "correlationId": "optional-caller-supplied-string"
}
```

The run is now executing in the background. Nothing about its outcome is known yet.

`runId` is the key for every later poll, and the `Location` response header repeats it. Its
value is either the hosting gateway's own invocation id — adopted when the platform supplies
one, because the gateway validates the id format on `GET` and rejects anything that is not
its own — or a locally generated UUID. Either way it always matches
`^[A-Za-z0-9._-]{1,128}$`; a platform id outside that shape is ignored in favour of a
generated one, and an id already in use by another run is never reused. Treat it as opaque:
do not parse it, and do not assume it is a UUID.

---

## 5. Status — `GET /invocations/{runId}`

```json
{
  "runId": "690cd3bc-4098-4dff-a94a-e275088f3738",
  "status": "succeeded",
  "phase": "done",
  "startedAt": "2026-09-09T06:45:42.702135Z",
  "finishedAt": "2026-09-09T06:45:42.892770Z",
  "result": {
    "pullRequestId": 812,
    "pullRequestUrl": "https://dev.azure.com/contoso/Platform/_git/payments-api/pullrequest/812",
    "branch": "bugfix/4123-null-ref",
    "commits": 3,
    "filesChanged": 7
  },
  "error": null,
  "correlationId": "optional-caller-supplied-string"
}
```

| Field | Notes |
|---|---|
| `status` | `queued` \| `running` \| `succeeded` \| `failed`. |
| `phase` | `fetching_work_item` \| `cloning` \| `coding` \| `pushing` \| `creating_pr` \| `done`. Show this to users; `coding` is where the minutes go. |
| `startedAt` / `finishedAt` | ISO-8601 UTC. `finishedAt` is `null` until the run reaches a terminal state. |
| `result` | Present only when `status` is `succeeded`, `null` otherwise. |
| `error` | Present only when `status` is `failed`, `null` otherwise. |
| `correlationId` | Exactly what you sent, or `null`. |

An unknown run id is a `404`.

Poll every 5 seconds. A run is terminal when `status` is `succeeded` or `failed`; nothing
changes after that.

---

## 6. Error codes

Exhaustive for v1. New codes are a breaking change.

| Code | Meaning | Retryable? |
|---|---|---|
| `work_item_not_found` | No such work item, or the work item type is not one the project defines. | No — fix the request. |
| `repository_not_found` | No such repository, or no such base branch. | No. |
| `branch_exists` | The target branch already exists on the remote. | No — pick another name. |
| `ado_forbidden` | The organization is not in `T2PR_ALLOWED_ORGS`, or the agent's Azure DevOps Personal Access Token lacks the scope for what was asked (or has expired). | No — a configuration or permissions fix. |
| `clone_failed` | The shallow clone did not complete. | Sometimes — transient network. |
| `harness_failed` | The coding CLI exited non-zero, or changed more than `T2PR_MAX_FILES_CHANGED` files. | Sometimes — read `message`. |
| `harness_no_changes` | The harness ran successfully and changed nothing. | No — the prompt or ticket needs work. |
| `push_failed` | The push was rejected. | Sometimes. |
| `pr_failed` | The branch pushed but the pull request could not be created. **The remote branch is deleted before this is reported**, so nothing dangles — with one deliberate exception: if creation failed *ambiguously* (a timeout, where Azure DevOps may have created the pull request anyway) the agent checks first and leaves the branch alone if a pull request exists on it. Deleting the source branch of a live pull request is the worse outcome. | Sometimes. |
| `timeout` | The harness exceeded `T2PR_MAX_RUN_MINUTES`. | Yes, with a larger budget or a smaller ticket. |
| `internal` | Anything else. | Report it. |

Failure example:

```json
{
  "runId": "3f21...",
  "status": "failed",
  "phase": "coding",
  "startedAt": "2026-09-09T06:45:42.702135Z",
  "finishedAt": "2026-09-09T06:52:10.001222Z",
  "result": null,
  "error": {
    "code": "harness_no_changes",
    "message": "the coding harness made no changes to the working tree"
  },
  "correlationId": null
}
```

---

## 7. Worked example with curl

```bash
ACCOUNT=contoso-foundry
PROJECT=agents
AGENT=ticket-to-pr
BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}/agents/${AGENT}/endpoint/protocols/invocations"

TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)
```

Start a run:

```bash
curl -s -X POST "${BASE}?api-version=v1" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "1",
    "organization": "contoso",
    "project": "Platform",
    "workItem": { "type": "Bug", "id": 4123 },
    "repository": "payments-api",
    "branch": "bugfix/4123-null-ref",
    "baseBranch": "main",
    "correlationId": "demo-1"
  }'
```

```json
{"runId":"690cd3bc-4098-4dff-a94a-e275088f3738","status":"queued","correlationId":"demo-1"}
```

Poll until terminal:

```bash
RUN=690cd3bc-4098-4dff-a94a-e275088f3738
while true; do
  BODY=$(curl -s -H "Authorization: Bearer $TOKEN" "${BASE}/${RUN}?api-version=v1")
  STATUS=$(printf '%s' "$BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  PHASE=$(printf '%s' "$BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')
  echo "$STATUS ($PHASE)"
  case "$STATUS" in succeeded|failed) printf '%s\n' "$BODY" | python3 -m json.tool; break;; esac
  sleep 5
done
```

To try the same requests with no Azure tenant at all, run the agent against the local
Azure DevOps stub — see
[`agent/README.md`](../agent/README.md#3-run-it-locally-end-to-end). There the base URL is
just `http://127.0.0.1:8010/invocations` and there is no `Authorization` header.

---

## 8. Client guidance

- **Poll at 5 s.** Coding runs take minutes; anything faster is wasted.
- **Send a `correlationId`** and log it. It is the only thing tying your request to the
  agent's logs.
- **Treat `phase` as advisory** for the UI, `status` as the truth.
- **Handle `pr_failed` as "almost always nothing left behind."** The agent deletes the
  remote branch before reporting it, so a retry with the same branch name is normally safe.
  The exception is deliberate: when pull request creation failed *ambiguously* — a timeout,
  where Azure DevOps may have created one anyway — the agent checks, and leaves the branch
  alone if a pull request exists on it, because deleting the source branch of a live pull
  request is the worse outcome. A retry that comes back `branch_exists` after a `pr_failed`
  means exactly that: look at the branch before you delete it.
- **Do not retry a `400`.** The body will not become valid on its own.
- **One run per work item at a time.** Two concurrent runs on the same branch name means
  the second gets `branch_exists`.
