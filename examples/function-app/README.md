# Sample webhook receiver (Azure Functions)

A minimal Azure DevOps **service-hook receiver** for the ticket-to-pr agent. Tag a work
item `agent`, and this Function App starts an agent run, comments the run id on the work
item, then comments the pull request link when the run finishes.

It exists to show the [request/response contract](../../docs/contract.md) being driven by
something other than the ADO extension. It is a sample: read it, copy it, change it.

```
work item tagged "agent"
        │  Azure DevOps service hook (workitem.created)
        ▼
  service_hook (HTTP trigger)   ──POST /invocations──►  ticket-to-pr agent
        │  comment: "started run <id>"                        │
        ▼                                                     │
   table storage (pending runs)                               │
        │                                                     │
  poll_runs (timer, every minute) ──GET /invocations/{id}──────┘
        │
        ▼
   comment: "opened pull request #812"
```

## What it does

| Function | Trigger | Behaviour |
|---|---|---|
| `service_hook` | `POST /api/service-hook` | Filters on the `agent` tag, builds the contract body, calls the agent, records the run, comments the run id. |
| `poll_runs` | Timer, every minute | Polls each pending run and comments the PR link or the failure, then forgets the run. |

Work items without the opt-in tag get a `200 ignored` — the service hook is not retried
and nothing else happens.

### How the contract body is built

| Contract field | Source |
|---|---|
| `organization` | Parsed from `resource.url` in the payload (`https://dev.azure.com/{org}/...`), or the `T2PR_ADO_ORGANIZATION` app setting. |
| `project` | The `System.TeamProject` field. |
| `workItem.type` / `workItem.id` | `System.WorkItemType` and `resource.id`. |
| `repository` | The `Custom.Repository` field (name configurable), else a `repo:<name>` tag. |
| `branch` | `{type-slug}/{id}-{title-slug}`, e.g. `bug/4123-null-reference-when-charging`. |
| `baseBranch` | The `Custom.BaseBranch` field if you have one; otherwise omitted and the agent uses the repo default. |
| `correlationId` | `service-hook-{id}`. |

There is deliberately no harness or model in the body. Those are agent deploy-time
configuration and the contract rejects them with a 400.

## Deploy it

### 1. Create the resources

```bash
RG=rg-ticket-to-pr
LOCATION=westus3
STORAGE=stticket2pr$RANDOM
APP=func-ticket-to-pr-$RANDOM

az group create --name "$RG" --location "$LOCATION"

az storage account create \
  --name "$STORAGE" --resource-group "$RG" --location "$LOCATION" \
  --sku Standard_LRS --allow-blob-public-access false

az functionapp create \
  --name "$APP" --resource-group "$RG" \
  --storage-account "$STORAGE" \
  --consumption-plan-location "$LOCATION" \
  --runtime python --runtime-version 3.12 --functions-version 4 \
  --os-type Linux \
  --assign-identity '[system]'
```

### 2. Give the identity what it needs

Three grants, one per thing the app talks to.

```bash
PRINCIPAL=$(az functionapp identity show --name "$APP" --resource-group "$RG" \
  --query principalId -o tsv)
SUB=$(az account show --query id -o tsv)
```

**a. Table storage** — the run bookkeeping table:

```bash
az role assignment create \
  --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Storage Table Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$STORAGE"
```

**b. The agent** — permission to invoke the agent endpoint. Assign **Foundry Agent
Consumer** (the least-privilege role for callers; it was previously named
`Azure AI User`, and that role also works) at the Foundry project scope:

```bash
az role assignment create \
  --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Foundry Agent Consumer" \
  --scope "/subscriptions/$SUB/resourceGroups/<foundry-rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
```

**c. Azure DevOps** — the app comments on work items with its own identity, so it needs
to be a user in the organization with **Basic** access and, on the project,
**View work items** and **Edit work items in this node**. That is the same procedure the
agent identity goes through; see [docs/deploy.md](../../docs/deploy.md#3-azure-devops-access-create-a-pat)
and use the Function App's principal id in place of the agent's.

### 3. Configure app settings

```bash
az functionapp config appsettings set --name "$APP" --resource-group "$RG" --settings \
  T2PR_AGENT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>/agents/ticket-to-pr/endpoint/protocols/invocations" \
  T2PR_STATE_ACCOUNT_URL="https://$STORAGE.table.core.windows.net"
```

| Setting | Required | Default | Notes |
|---|---|---|---|
| `T2PR_AGENT_ENDPOINT` | yes | — | The agent's invocations URL. After `azd up`, read it from the azd environment as `AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT`, or from `azd ai agent show`. |
| `T2PR_STATE_ACCOUNT_URL` | yes | — | Table endpoint of the storage account, e.g. `https://mystorage.table.core.windows.net`. |
| `T2PR_STATE_TABLE` | no | `ticketToPrRuns` | Table name. Created on first use. |
| `T2PR_OPT_IN_TAG` | no | `agent` | The tag that opts a work item in. |
| `T2PR_REPO_FIELD` | no | `Custom.Repository` | Reference name of a field carrying the repo name. |
| `T2PR_ADO_ORGANIZATION` | no | parsed from payload | Set this for `*.visualstudio.com` or Azure DevOps Server hosts. |

### 4. Publish

```bash
cd examples/function-app
func azure functionapp publish "$APP"
```

(`func` is the [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local).)

Get the invoke URL, which includes the function key:

```bash
func azure functionapp list-functions "$APP" --show-keys
```

## Configure the service hook

In Azure DevOps: **Project settings → Service hooks → Create subscription**.

1. Service: **Web Hooks**. Next.
2. Trigger on: **Work item created**. Next.
   *(Also create a second subscription on **Work item updated** with a filter on
   `Tags` if you want to opt in an existing ticket by adding the tag later.)*
3. Filters: leave **Area path** and **Work item type** as `[Any]`. Tag filtering happens
   in the function, not here, because the service-hook tag filter only fires on tag
   *changes*.
4. Action settings:
   - **URL**: the `service_hook` invoke URL from step 4, including `?code=...`.
   - **Resource details to send**: **All**. The function needs `resource.fields`.
   - **Messages to send** / **Detailed messages to send**: **None** (not used).
5. **Test** and then **Finish**.

Now create a work item, add the tag `agent` and a tag `repo:payments-api`, and watch the
comments appear.

## Local development

```bash
cd examples/function-app
python -m venv .venv && .venv/bin/pip install -r requirements.txt
az login   # DefaultAzureCredential picks this up
```

Create `local.settings.json` (gitignored — never commit it):

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "T2PR_AGENT_ENDPOINT": "https://<account>.services.ai.azure.com/api/projects/<project>/agents/ticket-to-pr/endpoint/protocols/invocations",
    "T2PR_STATE_ACCOUNT_URL": "https://<storage>.table.core.windows.net"
  }
}
```

```bash
func start
```

Then replay a captured service-hook body:

```bash
curl -s -X POST http://localhost:7071/api/service-hook \
  -H 'Content-Type: application/json' -d @sample-payload.json
```

## Lint and type-check

The sample is held to the same bar as `agent/`:

```bash
cd examples/function-app
ruff check .
mypy --strict .
```

## Notes and limitations

- **`poll_runs` scans the whole table each minute.** Fine for tens of concurrent runs.
  For more, partition by day or move to a durable orchestration.
- **A run that never reaches a terminal state stays in the table forever.** Add a
  `startedAt` age check and give up after, say, an hour.
- **Comment failures are logged, not retried.** The run still completes; you just do not
  get the comment. That is deliberate: the pull request is the real output.
- **The HTTP trigger uses a function key** (`authLevel: FUNCTION`). The key is the only
  thing standing between the internet and this endpoint, so treat the service-hook URL as
  a secret. If you need better, put the function behind Entra ID authentication and
  configure the service hook with a basic-auth header instead.
