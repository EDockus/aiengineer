# Deploying the ticket-to-pr agent

An Azure subscription and an Azure DevOps organization in, a working agent out.

```bash
git clone https://github.com/EDockus/aiengineer.git
cd aiengineer
./scripts/setup.sh
```

That is the deployment. Budget about an hour of wall clock, most of it waiting for Azure,
and about ten minutes of your attention answering prompts.

**`azd up` on its own is not enough.** Nine things sit outside it, and every one of them
was found the same way — by a deploy that failed with an error naming none of them.
`scripts/setup.sh` does all nine, is safe to re-run, and prints what it is doing.
[Appendix A](#appendix-a--what-setupsh-does-and-how-to-do-it-by-hand) is the same nine
steps written out as commands, for people who would rather understand them, automate them
differently, or run them under a different identity.

**Contents**

1. [Prerequisites](#1-prerequisites)
2. [Configure the three parameters](#2-configure-the-three-parameters)
3. [Azure DevOps access: create a PAT](#3-azure-devops-access-create-a-pat)
4. [Deploy](#4-deploy)
5. [Let callers invoke the agent](#5-let-callers-invoke-the-agent)
6. [Verify](#6-verify)
7. [Local development with `azd ai agent run`](#7-local-development-with-azd-ai-agent-run)
8. [Troubleshooting](#8-troubleshooting)
9. [Tearing it down](#9-tearing-it-down)
- [What this costs](#what-this-costs)
- [Appendix A — what `setup.sh` does, and how to do it by hand](#appendix-a--what-setupsh-does-and-how-to-do-it-by-hand)
- [Appendix B — facts this deployment established](#appendix-b--facts-this-deployment-established)

> **On the two harnesses — decide this first.** `claude-code` is the better-supported
> option today, and it is the default. Microsoft's Codex-on-Foundry page (ms.date
> 2026-09-03) states in its troubleshooting table: *"Entra ID support is currently not
> available for Codex."* The only authentication it documents for Foundry is an API key,
> and this project uses no keys. **A 401 from the Codex harness is expected** until
> [openai/codex#2024](https://github.com/openai/codex/issues/2024) ships. Claude Code's
> Foundry path (`CLAUDE_CODE_USE_FOUNDRY=1` + `ANTHROPIC_FOUNDRY_BASE_URL`, falling
> through to `azureADTokenProvider` when no key is set) is documented, supported, and is
> what this deployment was verified against end to end. Details in
> [configuration.md §4](configuration.md#4-harness-configuration--what-was-verified).

---

## 1. Prerequisites

### Accounts and roles

| You need | Where | Why |
|---|---|---|
| **Contributor** | Subscription, or the target resource group | Create the Foundry account, project, registry and Application Insights. |
| **User Access Administrator** or **Owner** | The same scope | `setup.sh` creates four role assignments (two AcrPull, one model role, plus whatever you grant callers). Contributor alone cannot assign roles. |
| **Foundry Project Manager** | The Foundry project | Create and deploy a hosted agent, and write project connections. |
| **Application Administrator** (or ownership of the app) | Entra tenant | Create the extension's SPA app registration and grant admin consent. Step 8 tells you exactly what to hand an administrator if you do not have this. |
| A member account in the Azure DevOps organization | Azure DevOps | Mint the PAT. The PAT inherits that account's repository access. |
| Model quota | Your target region | Both `claude-opus-5` and `gpt-6` need quota; some subscription tiers must request it. |

> The Foundry RBAC roles were renamed: **Foundry User / Owner / Account Owner / Project
> Manager** were previously **Azure AI User / Owner / Account Owner / Project Manager**.
> The role IDs and permissions are unchanged, and you will see both names in portals for
> a while. Where this page says `Foundry User`, `Azure AI User` is the same role.

### Tools

`scripts/setup.sh` checks all of these in its first step and names the fix for whichever
is missing, so you can also just run it and see. **Node is the one soft check**: it is
needed only to build the Azure DevOps extension in step 9, so a host without it gets a
working agent and the four commands to run the build somewhere else.

```bash
az version                    # Azure CLI 2.80+
azd version                   # Azure Developer CLI 1.27.1+
node --version                # optional: step 9 builds the extension .vsix with it
python3 --version             # used for JSON handling; no jq dependency anywhere
git --version
curl --version                # mints and revokes the PAT; absent from slim base images
docker --version              # optional: azd builds the image remotely inside ACR
```

### Resource providers

Nothing in `azd` registers a resource provider, and a subscription that has never run a
Foundry or `azd` workload commonly has `Microsoft.CognitiveServices` unregistered. Step 1
checks all four and offers to register them; the failure it prevents is
`MissingSubscriptionRegistration` from `azd provision`, which reads like a missing role or
an unsupported region and is neither.

```bash
for NS in Microsoft.CognitiveServices Microsoft.ContainerRegistry \
          Microsoft.OperationalInsights Microsoft.MachineLearningServices; do
  printf '%-36s %s\n' "$NS" "$(az provider show -n "$NS" --query registrationState -o tsv)"
done
```

Registration is subscription-wide, idempotent and takes a minute or two per namespace. It
needs Contributor or Owner on the subscription.

Install `azd` if you need to:

```bash
brew tap azure/azd && brew install azd            # macOS
curl -fsSL https://aka.ms/install-azd.sh | bash   # Linux
winget install microsoft.azd                      # Windows
```

Then the Foundry extensions. `microsoft.foundry` is a meta-package that pulls in
`azure.ai.projects`, `azure.ai.agents` and `azure.ai.connections`, all three of which
this deployment uses:

```bash
azd extension install microsoft.foundry
azd extension list --installed
```

`azure.yaml` declares the minimums under `requiredVersions`, so `azd` tells you if you
are behind. Note the floor on `azure.ai.agents` is **`1.0.0-beta.13`**, not `.14`: at the
time of writing `.14` is the latest *available* version, and requiring a version that
does not exist yet makes `azd deploy` fail its constraint check before doing any work.

### One login, not two

```bash
az login
azd config set auth.useAzCliAuth true
```

With `auth.useAzCliAuth` set, `azd` reuses the Azure CLI's token cache instead of running
its own browser login. `setup.sh` sets it for you. Without it you sign in twice, and the
two sessions can end up on different tenants, which produces confusing 403s later.

---

## 2. Configure the three parameters

Three settings are deployment decisions rather than file constants, so `azure.yaml`
references them as `${...}` into the `azd` environment. `setup.sh` step 2 prompts for
all three; these are the same values, set by hand:

```bash
azd env set T2PR_HARNESS          claude-code
azd env set T2PR_MODEL_DEPLOYMENT claude-opus-5
azd env set T2PR_ALLOWED_ORGS     contoso
```

| Variable | What it is |
|---|---|
| `T2PR_HARNESS` | `claude-code` or `codex`. See the note at the top of this page. Default `claude-code`. |
| `T2PR_MODEL_DEPLOYMENT` | The **deployment name** on the Foundry account — the name you chose, not the catalog model id. The harness passes it as `--model`. |
| `T2PR_ALLOWED_ORGS` | Comma-separated Azure DevOps organizations the agent will act on. **An empty allowlist rejects every request and the agent refuses to start**, so this is not optional. |

These land in `.azure/<env>/.env`, which is gitignored. Check them with
`azd env get-values`. Everything else in [configuration.md](configuration.md) is a
literal in `azure.yaml`'s `env:` map — edit it there and redeploy.

### Why the model deployment is not created by `azure.yaml`

`azure.yaml`'s `ai-project` service has a `deployments:` block, and this repository does
not use it. Two reasons, both verified with `azd provision --preview` on 2026-09-10:

1. **`${VAR}` is not substituted anywhere inside an `azure.ai.project` service.** A
   deployment declared as `name: ${T2PR_MODEL_DEPLOYMENT}` is planned as a model
   deployment literally called `${T2PR_MODEL_DEPLOYMENT}`. Substitution works in the
   *agent* service's `env:` map and (per the extension's own debug logging) under
   `network:` — nowhere else. Every value in that service must be a literal.
2. **ARM refuses Anthropic deployments from that block.** The preflight error is
   `InvalidModelProviderData: ModelProviderData is required for Anthropic model
   deployments but was not provided. Please provide all required fields: industry,
   organizationName, and countryCode.` The `deployments` schema in `azure.yaml` has no
   field for `modelProviderData`, and neither does
   `az cognitiveservices account deployment create`.

So `setup.sh` creates the deployment with a direct ARM `PUT`, asking for the three
Marketplace fields when the format is Anthropic. They are not secret, are not validated,
and exist so the resource provider can auto-sign the Azure Marketplace offer for you.

If you are on the `codex` harness and want an OpenAI deployment created by `azd` instead,
`azure.yaml` carries a commented-out block to uncomment — with the deployment name
hard-coded, per point 1.

---

## 3. Azure DevOps access: create a PAT

The agent authenticates to Azure DevOps with a **Personal Access Token**. Foundry models
still use the agent's own Entra identity — only Azure DevOps uses a secret.

### Why not the agent identity — the decision record

**This is the reference record for the PAT decision. Read it before changing how the agent
authenticates to Azure DevOps.** Everything else in the repository that mentions the PAT —
`ado.py`, `config.py`, `SECURITY.md`, [architecture.md §1](architecture.md#1-the-whole-system)
— points here.

The original design had the agent authenticate to Azure DevOps with its own Entra agent
identity and no secret at all. That is not achievable on a Foundry hosted agent today.
Four routes were tried against a live tenant and a live organization on **2026-09-09**:

| Route | Result |
|---|---|
| Agent identity added to the Azure DevOps organization | `VS403283: Could not add user`. An ordinary service principal, entitled minutes later in the *same* organization with the *same* API call, **succeeded** — so this is the identity type, not the organization, not the licences (49,990 free ones were available), and not the call. |
| Agent identity federated into an app registration Azure DevOps does accept | `AADSTS700231: Token obtained using a federated identity credential may not be used as a federated identity credential.` Foundry's agent-identity token is itself FIC-derived, so it can never be the assertion in another federation. Chaining federated identity credentials is forbidden by Entra, not merely unsupported here. |
| A user-assigned managed identity on the container | No attachment point. [Microsoft's agent identity docs](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity) describe a fixed chain: project managed identity → blueprint → agent identity. There is nowhere to attach one. |
| A user-assigned managed identity added to the Azure DevOps organization | **Succeeded** — but a hosted agent cannot run as one. That route requires Azure Container Apps or AKS instead of a Foundry hosted agent. |

**Root cause.** The agent-identity model assumes downstream services authorize by **Azure
RBAC** — the documented audiences are Storage, Cosmos DB, Key Vault and Graph. Azure DevOps
authorizes by **organization membership**, which it will not grant an agent identity.

**The consequence.** "Foundry hosted agent" and "no keys anywhere" are mutually exclusive on
the current platform. This project keeps the hosted-agent requirement and accepts a PAT for
Azure DevOps only. Foundry model access is unaffected and remains keyless.

**Before you "fix" this.** Do not switch Azure DevOps back to `DefaultAzureCredential`
without re-running the four routes above and recording what changed. The failure mode is not
a compile error or a test failure — it is a `VS403283` at deploy time, or worse, a silent
401 in production. All Azure DevOps auth sits behind one token provider in
`agent/src/ticket_to_pr/ado.py`, so the swap is small on the day Azure DevOps starts
accepting Entra agent identities. Until then it is not a swap that can succeed.

### 3.1 The three scopes

Whether you let `setup.sh` mint the token or create it in the UI, it needs exactly these
and nothing else:

| Scope | Access | Why |
|---|---|---|
| Work Items | Read | Fetch the work item, its type and linked item titles. |
| Code | Read & Write | Clone the base branch, push the new one. |
| Pull Request | Read & Write | Create the pull request and add reviewers. |

No *Full access*, no build, no release, no packaging, no wiki. These are the PAT
equivalent of the four repository permissions the agent actually uses. In the token
lifecycle API the same three are the scope string `vso.work vso.code_write` —
`vso.code_write` covers cloning, pushing *and* pull requests; there is no separate PR
scope.

Scope the token to the single organization in `T2PR_ALLOWED_ORGS` — not "all accessible
organizations" — and set the shortest expiry your team can operate with. Create it under
an account with access to the target repositories; the PAT inherits that account's
permissions, capped by the scopes above. A dedicated service account is better than a
person's: it survives them leaving, and its access is auditable as the agent's rather
than theirs.

### 3.2 `setup.sh` can mint it for you

Step 5 offers two routes:

- **Mint it.** `setup.sh` gets an Entra token for the Azure DevOps resource
  (`499b84ac-1321-427f-aa17-267ca6975798`) from your `az login` and calls the token
  lifecycle API:
  `POST https://vssps.dev.azure.com/{org}/_apis/tokens/pats?api-version=7.1-preview.1`
  with `{"scope": "vso.work vso.code_write", "allOrgs": false, "validTo": ...}`. It never
  prints the token. It does record the returned `authorizationId` in the `azd`
  environment — that is not a secret, and it is what `teardown.sh` uses to revoke the
  token later.
- **Paste one in.** Input is hidden. Use this if your organization's policy forbids
  programmatic PAT creation, or if you want the token to belong to a service account
  rather than to you.

### 3.3 Where the token is stored

Not in `.azure/<env>/.env`. It goes into a **Foundry project connection**, which stores
it in the project's Key Vault:

```bash
azd ai connection create ado-secrets --kind remote-tool \
  --target "https://dev.azure.com/<org>" --auth-type custom-keys \
  --custom-key "pat=<token>" --force
```

That is better than an `azd` variable in three ways: the token never lands on a developer
machine, it is never written into the agent version's configuration, and a `GET` on the
agent version returns a placeholder rather than the secret. Rotate by updating the
connection — no redeploy needed, because the container resolves the placeholder at start.

One caveat worth knowing: the token is on that command line, so on a shared machine it is
briefly visible to `ps`. The azd connections CLI takes no stdin alternative. Run setup on
a machine you trust.

### 3.4 The `${{connections...}}` indirection

This looks like a mistake in `azure.yaml` and is not.

`azure.yaml` **cannot** contain the placeholder directly:

```yaml
T2PR_ADO_PAT: ${{connections.ado-secrets.credentials.pat}}   # azd rejects this
```

`azd` runs its own `${VAR}` substitution over `azure.yaml` before the platform ever sees
it, and it cannot parse the doubled-brace form — it fails with **`unable to parse
variable name`**. So the indirection is:

- `azure.yaml` holds `T2PR_ADO_PAT: ${T2PR_ADO_PAT}`, an ordinary azd variable.
- The azd environment variable `T2PR_ADO_PAT` holds the **literal placeholder text**
  `${{connections.ado-secrets.credentials.pat}}`.
- `azd` copies that text through verbatim into the agent version.
- The platform resolves it against the Foundry connection when the container starts.

`setup.sh` step 6 sets it:

```bash
azd env set T2PR_ADO_PAT '${{connections.ado-secrets.credentials.pat}}'
```

Note the single quotes. The azd environment therefore holds a harmless reference, never
the token.

### 3.5 What the agent does with the token

- Sent as HTTP `Basic` (Azure DevOps takes a PAT as the password of an empty-username
  pair), never `Bearer`.
- Handed to `git` through `GIT_CONFIG_COUNT` environment variables — never argv, which is
  world-readable via `ps`, and never written into `.git/config`.
- Excluded from the harness environment: the allowlist in `harness/__init__.py` drops
  every `T2PR_*` variable, and the harness runs as a different uid from the server.
- Registered with the redactor at startup, so it is struck out of harness output, git
  stderr, log lines, `error.message` payloads and pull request bodies — by value, not by
  shape. See `agent/tests/test_redact.py`.

### 3.6 Rotation and revocation

Rotate by minting a new token and updating the connection:

```bash
azd ai connection create ado-secrets --kind remote-tool \
  --target "https://dev.azure.com/<org>" --auth-type custom-keys \
  --custom-key "pat=<new token>" --force
```

The container reads the value at startup, so a rotation still needs a restart — but not a
rebuild. Revoke the old token in Azure DevOps afterwards.

Revoke immediately if a token appears in a log, a screenshot, a shared terminal or a
commit. Revocation is instant, and the agent will then fail every run with
`ado_forbidden`, which is the correct outcome.

---

## 4. Deploy

```bash
./scripts/setup.sh
```

It runs nine steps and prints a banner for each. Any of them can be re-run on its own:

```bash
./scripts/setup.sh --list        # the nine steps
./scripts/setup.sh --from 7      # resume at the deploy step
./scripts/setup.sh --only 4      # just the container registry step
```

| Step | What it does | Why `azd up` does not |
|---|---|---|
| 1 | Tools, versions, **resource-provider registration**, `az`/`azd` login, `auth.useAzCliAuth` | Nothing in azd registers a resource provider; on a fresh subscription `Microsoft.CognitiveServices` is commonly unregistered. |
| 2 | azd environment, subscription, location, the three parameters | `azd ai agent init`'s wizard has no mechanism for a template to add its own prompts. |
| 3 | Create a Foundry project, or connect to an existing one; check quota, then create the model deployment | Connecting needs `AZURE_AI_PROJECT_ID`, which azd will not derive; Anthropic deployments need `modelProviderData`, which `azure.yaml` cannot express. |
| 4 | Container registry, and **AcrPull for both** the account identity and the project identity | azd's own Bicep grants the project identity only. |
| 5 | Mint or accept the PAT and store it as a Foundry connection | — |
| 6 | The `${{connections...}}` placeholder variable | `azure.yaml` cannot hold the form directly. |
| 7 | First `azd deploy`, then a model role for the agent's instance identity | azd assigns the agent identity **nothing**. |
| 8 | The Entra SPA app registration for the Azure DevOps extension | Not an azd concern at all. |
| 9 | Records the Marketplace publisher and repository URL, **builds the extension `.vsix`**, prints the upload steps and a teardown summary | — |

### Why the PAT comes before the deploy

A hosted agent's environment variables are **baked into the agent version** at deploy time.
`azd ai agent show` returns them under `definition.environment_variables`, and nothing
re-reads the azd environment afterwards. `config.py` refuses to start when `T2PR_ADO_PAT`,
`T2PR_HARNESS`, `T2PR_MODEL_DEPLOYMENT` or `T2PR_ALLOWED_ORGS` is missing — so a version
deployed before the PAT exists never answers `/readiness`, never reaches `active`, and
cannot be repaired by anything except deploying again.

Steps 5 and 6 are cheap, local and reversible; step 7 is the expensive one. So step 7 runs
last, and it enforces that ordering rather than trusting it:

- It **refuses to run** unless all four of those variables are non-empty, naming the ones
  that are not and which step sets them. That check runs on `--only 7` and `--from 7` too,
  which are exactly the paths that skip the steps that set them.
- It also refuses when step 3 recorded that you declined to create the model deployment
  (`T2PR_SETUP_MODEL_DEPLOYMENT_MISSING=true`) — deploying then produces an agent that
  404s on every run for the life of the version.
- It **re-deploys by itself** when the deployed version's baked variables no longer match
  the azd environment, or when step 6 recorded the placeholder after the last deploy. A
  run interrupted between steps 6 and 7 therefore heals on the next invocation instead of
  leaving a wedged version behind. An ordinary re-run where nothing changed does not
  redeploy.

First run takes 10–20 minutes, mostly the model deployment and the container build. Later
`azd deploy` runs take a couple of minutes.

```bash
azd ai agent show                       # name, version, protocols, resources, env
azd ai agent show --output table
azd ai agent monitor                    # live container logs
azd env get-values | grep AGENT_        # the endpoint URLs azd wrote back
azd ai agent doctor                     # 11 local and remote health checks
```

The invocations URL you will use everywhere else is
`AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT`. **It already carries `?api-version=v1`.** Keep
it — see [§6](#6-verify).

### Create or connect

`azure.yaml` expresses both, and the switch is a single line: the `endpoint:` under the
`ai-project` service. `setup.sh` writes it.

| | `endpoint:` commented out | `endpoint:` present |
|---|---|---|
| `azd provision` plans | resource group, Foundry (AI Services) account, project, container registry, ACR connection | the ACR connection only |
| `azd down` | deletes the resource group | leaves the existing project alone |

It has to be a literal, because of the no-`${VAR}` finding in
[§2](#why-the-model-deployment-is-not-created-by-azureyaml). That is why `setup.sh`
rewrites the line rather than setting a variable.

Connecting to an existing project additionally needs three values in the azd environment,
none of which azd derives for you:

```bash
azd ai project set "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_PROJECT_ID "/subscriptions/.../accounts/<account>/projects/<project>"
azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT    "<registry>.azurecr.io"
azd env set AZURE_CONTAINER_REGISTRY_RESOURCE_ID "/subscriptions/.../registries/<registry>"
```

Without the first: `AZURE_AI_PROJECT_ID does not identify the project configured by the
azure.yaml endpoint`. Without both registry values: `reuse-connect requires both container
registry endpoint and resource ID`. Both verified 2026-09-10.

### What `azure.yaml` declares

| | |
|---|---|
| Provisioning provider | `infra.provider: microsoft.foundry`. Without it `azd provision` falls back to the built-in Bicep provider and fails with `Could not find a part of the path '<repo>/infra/main.bicep'` — this repository ships no `infra/`. |
| Protocol | `invocations` version `1.0.0` — the agent defines its own JSON payload; see [contract.md](contract.md). |
| Resources | `cpu: "1"`, `memory: 2Gi`, one of the three supported sandbox sizes. |
| Idle timeout | `sessionConfiguration.idleTimeoutSeconds: 1800` (30 minutes; the platform allows 120–3600, default 900). |
| Environment | Everything in [configuration.md §1](configuration.md#1-settings), including `T2PR_PORT: "8088"`. |
| Not declared | `FOUNDRY_PROJECT_ENDPOINT`. The platform injects it; declaring it in the container's `env` map shadows the platform value. |
| Also not declared | `ENABLE_HOSTED_AGENTS` and `ENABLE_CAPABILITY_HOST`. They appear in a working `.env` and in no file in this repository, which looks like a missing step. It is not one: the `azure.ai.agents` extension writes both itself, hard-coded, from its `preprovision`/`predeploy` hooks whenever the agent definition is hosted — and under `infra.provider: microsoft.foundry` nothing reads them anyway. See [Appendix B](#appendix-b--facts-this-deployment-established). |

> **Why protocol version `1.0.0` and not `2.0.0`.** On container protocol 1.0.0 the
> platform propagates caller identity to Foundry for you. On 2.0.0 the container must read
> a per-request `x-agent-foundry-call-id` header and forward it on outbound Foundry calls,
> which this container does not yet do. 1.0.0 is marked deprecated upstream and will
> eventually be blocked, so this is a known follow-up: teach the agent to forward that
> header, then change the one line in `azure.yaml`.

---

## 5. Let callers invoke the agent

Anything invoking the agent — you with curl, the ADO extension, the sample Function App —
needs a data-plane role. **Foundry Agent Consumer** is the least-privilege one;
`Foundry User` (formerly `Azure AI User`) also works and is what you want if the same
principal also manages agents. `setup.sh` does not do this for you: who may use the agent
is a decision, not a deployment detail.

```bash
SUB=$(az account show --query id -o tsv)
FOUNDRY_RG=<resource group>
ACCOUNT=<foundry account name>
PROJECT=<foundry project name>
SCOPE="/subscriptions/$SUB/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT/projects/$PROJECT"

# a person
az role assignment create \
  --assignee "someone@contoso.com" \
  --role "Foundry Agent Consumer" \
  --scope "$SCOPE"
```

### Assigning it to a group

Grant the role once to an Entra group and manage membership there instead of handing out
individual assignments:

```bash
GROUP_ID=$(az ad group create \
  --display-name "Ticket-to-PR users" \
  --mail-nickname ticket-to-pr-users \
  --query id -o tsv)

az role assignment create \
  --assignee-object-id "$GROUP_ID" \
  --assignee-principal-type Group \
  --role "Foundry Agent Consumer" \
  --scope "$SCOPE"

az ad group member add --group "$GROUP_ID" \
  --member-id "$(az ad user show --id someone@contoso.com --query id -o tsv)"
```

In the portal: the Foundry **project** resource → **Access control (IAM)** → **Add role
assignment** → **Foundry Agent Consumer** → **Members: User, group, or service
principal** → pick the group.

You can scope the assignment to a single agent rather than the whole project if the
project hosts more than one.

### The agent's own model access — azd grants it nothing

This is the one that costs you a whole deploy cycle to discover.

The agent's Entra identity does not exist until the first deploy creates an agent version,
and **azd assigns it no roles at all**. Without a model role, the first real run dies
with:

```
401 Principal does not have access to API/Operation
```

`setup.sh` step 7 reads the principal id out of `azd ai agent show --output json`
(`instance_identity.principal_id`) and grants it a role on the Foundry **account** scope —
account, not project, because the Claude Code harness talks to the account-level
`/anthropic` route:

```bash
PRINCIPAL=$(azd ai agent show --output json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["instance_identity"]["principal_id"])')

az role assignment create \
  --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services User" \
  --scope "/subscriptions/$SUB/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"
```

> **This one role is enough**, and that is measured rather than assumed: every other role
> was removed from a working deployment and a full job — fetch, clone, code, push, pull
> request — still succeeded in 30 seconds.
>
> **If you get a 401 immediately after granting it, wait rather than granting more.** Role
> assignments take 30–150 seconds to take effect, and a retry inside that window is
> indistinguishable from an insufficient role. Diagnosing it that way is how this identity
> briefly collected three redundant roles. `Cognitive Services OpenAI User` and
> `Foundry User` remain listed in troubleshooting only as a fallback for a
> differently-configured tenant.

> **Account scope is wider than project scope.** It grants inference against *every* model
> deployment on that account, not just this project's. If that matters, give the agent its
> own Foundry account: account scope and project scope then coincide and the grant costs
> nothing extra. That is the recommended resolution, and it is why `setup.sh`'s create
> path makes a fresh account rather than sharing one.

---

## 6. Verify

> **Give it a moment first.** `azd deploy` — and so step 7 of `setup.sh` — reports success
> as soon as the agent *version* reaches `active`. The invocations endpoint starts serving
> a little later. A `POST` issued immediately after deploy can come back without a `runId`;
> observed once, and it resolved on its own within a couple of minutes. If your first call
> looks like that, wait and repeat it before believing anything is wrong. A run that
> genuinely fails returns a contract error code, not a malformed body.

```bash
BASE=$(azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT)
TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)

curl -si -X POST "$BASE" \
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
    "correlationId": "first-deploy"
  }'
```

Three things in that command are load-bearing, and each of them was learned from a
failure:

**The token audience is `https://ai.azure.com`, not `cognitiveservices.azure.com`.** Both
look plausible; only one works. The Cognitive Services audience comes back from the
Foundry model endpoint as:

```
401 ... audience is incorrect (https://ai.azure.com)
```

The agent uses the same scope internally — `FOUNDRY_SCOPE = "https://ai.azure.com/.default"`
in `agent/src/ticket_to_pr/harness/__init__.py` — and so does the browser extension.

**Every gateway call needs `?api-version=v1`.** `$BASE` as azd writes it already carries
it; do not strip the query string when you build URLs by hand. Without it the gateway
answers `400 Missing required query parameter: api-version`.

**Poll with the run id the gateway hands back, not the one in the body.** The response
carries an `x-agent-invocation-id` header, and that is the id
`GET /invocations/{id}?api-version=v1` accepts. Use anything else — including a run id the
agent generated itself — and the gateway rejects the request before it reaches the
container:

```
400 Invalid invocation ID format
```

That is why the curl above uses `-si`: you need the headers.

```bash
RUN_ID=<the x-agent-invocation-id value>
curl -s -H "Authorization: Bearer $TOKEN" \
  "${BASE%%\?*}/${RUN_ID}?api-version=v1" | python3 -m json.tool
```

Watch `phase` move `fetching_work_item → cloning → coding → pushing → creating_pr → done`.
The `coding` phase is where the minutes go. Full contract in
[contract.md](contract.md).

Tail the container while it runs:

```bash
azd ai agent monitor
```

---

## What this costs

The model is the bill. A **trivial** run — a small work item, a one-file change —
measured `total_cost_usd: 0.14` on `claude-opus-5` at list pricing. Real tickets read more
context and write more code; budget several times that per run and measure your own before
you promise anyone a number.

Everything else is close to free while idle. Compute costs nothing between runs — the
platform deprovisions the sandbox after `idleTimeoutSeconds` — and the registry, Log
Analytics and Application Insights are small fixed costs. A `GlobalStandard` model
deployment with no traffic is billed per token, not per hour.

**There is no native per-agent spend cap.** Foundry will not stop an agent at a dollar
figure. The two things that come close:

- **An APIM-based AI Gateway** in front of the model, with token-limit and quota policies.
  That is a real cap, and it is a real piece of infrastructure to run.
- **Azure Budgets** on the resource group or subscription. These **alert; they do not
  enforce.** A budget will email you after the money is spent, not before.

The controls this repository does give you are blunt but effective: `T2PR_MAX_RUN_MINUTES`
(default 20) bounds a single run, `T2PR_MAX_FILES_CHANGED` (default 50) bounds a runaway
one, and `T2PR_ALLOWED_ORGS` bounds who can start one at all. If cost is a hard
constraint, cap the model deployment's `capacity` (tokens per minute) — a low ceiling
makes a runaway slow and visible rather than expensive.

---

## 7. Local development with `azd ai agent run`

`azd ai agent run` starts the agent on your machine but wires it to your **real** Foundry
project, so the harnesses reach real models with your own `az login` credentials.

```bash
azd env select dev          # pick the environment whose Foundry project you want
azd ai agent run
```

It reads `startupCommand` from `azure.yaml`, injects every value from the active `azd`
environment — including `FOUNDRY_PROJECT_ENDPOINT` and the `env:` map with `${...}`
resolved — and serves on `localhost:8088`.

```bash
azd ai agent run --port 9090                     # if 8088 is taken
azd ai agent run --start-command "ticket-to-pr"  # override startupCommand
```

Drive it from a second terminal. Because the agent uses the `invocations` protocol, send
a body file and select the protocol explicitly:

```bash
cat > request.json <<'JSON'
{
  "version": "1",
  "organization": "contoso",
  "project": "Platform",
  "workItem": { "type": "Bug", "id": 4123 },
  "repository": "payments-api",
  "branch": "bugfix/4123-local",
  "correlationId": "local-dev"
}
JSON

azd ai agent invoke --local --protocol invocations -f request.json
```

or plain curl, since `--local` is just `localhost:8088`:

```bash
curl -s -X POST http://localhost:8088/invocations \
  -H 'Content-Type: application/json' -d @request.json
```

Locally there is no gateway, so neither `?api-version=v1` nor `x-agent-invocation-id`
applies — the run id in the response body is the one to poll with. That difference is the
usual reason a script that works locally 400s against the deployed agent.

Local runs still act on your real Azure DevOps organization with **your** identity, not
the agent's — so they can open real pull requests, and they will succeed even before the
PAT exists. Use a scratch repository.

For a loop with **no Azure at all** — no Foundry project, no ADO org, no model — use the
dev-mode path with the local ADO stub and the `fake` harness, described in
[`agent/README.md`](../agent/README.md#3-run-it-locally-end-to-end). Neither dev mode nor
the `fake` harness can be switched on in a deployed image; see
[configuration.md §3](configuration.md#3-dev-only-variables).

### Starting from scratch instead

If you are building your own agent rather than deploying this one, `azd ai agent init`
scaffolds a project and asks for: an agent template, the Azure subscription, an existing
Foundry project or a new one (and its region), and a model deployment. It records the
deployment name in the `azd` environment and creates an environment called
`<directory>-dev`. Use `--deploy-mode container` — this agent needs Node and two npm CLIs
in the image, which ZIP/source deployment cannot install.

Do **not** run `azd ai agent init` inside this repository: it scaffolds a *new* project
and would overwrite `azure.yaml`.

---

## 8. Troubleshooting

### Deployment

| Symptom | Cause | Fix |
|---|---|---|
| `Could not find a part of the path '<repo>/infra/main.bicep'` | `azure.yaml` has no `infra.provider`, so azd used the built-in Bicep provider. | Restore `infra: { provider: microsoft.foundry }` at the top level of `azure.yaml`. |
| **`MissingSubscriptionRegistration: The subscription is not registered to use namespace 'Microsoft.CognitiveServices'`** | **The resource provider is not registered on this subscription.** Nothing in azd registers one, and a fresh subscription commonly has this namespace unregistered. It is not a missing role and not a bad region, which is what the message reads like. | `./scripts/setup.sh --only 1` checks all four and offers to register them, or `az provider register -n Microsoft.CognitiveServices --wait` by hand. Registration is subscription-wide, idempotent, and takes a minute or two. |
| `AZURE_AI_PROJECT_ID does not identify the project configured by the azure.yaml endpoint` | Connecting to an existing project without telling azd its ARM id. | `azd env set AZURE_AI_PROJECT_ID /subscriptions/.../projects/<name>`, or `./scripts/setup.sh --only 3`. |
| `reuse-connect requires both container registry endpoint and resource ID` | Only `AZURE_CONTAINER_REGISTRY_ENDPOINT` is set. | Set `AZURE_CONTAINER_REGISTRY_RESOURCE_ID` too, or `./scripts/setup.sh --only 4`. |
| **`[ImageError] Container registry authentication failed`** | **AcrPull is missing on one of the two identities that pull the image.** azd's Bicep grants the *project* managed identity; the Cognitive Services *account* identity needs it as well. | `./scripts/setup.sh --only 4`. It grants both and is idempotent. Allow a minute for propagation. |
| `InvalidModelProviderData: ModelProviderData is required for Anthropic model deployments` | An Anthropic `deployments:` entry in `azure.yaml`. | Remove it. `setup.sh` creates Anthropic deployments with an ARM `PUT` that carries `organizationName`, `countryCode` and `industry`. |
| A model deployment appears named literally `${T2PR_MODEL_DEPLOYMENT}` | `${VAR}` is not substituted inside an `azure.ai.project` service. | Hard-code the name, or let `setup.sh` create the deployment. |
| `unable to parse variable name` | A `${{...}}` expression in `azure.yaml`. | Use the indirection in [§3.4](#34-the-connections-indirection). |
| `azd deploy` fails its version constraint before doing any work | `requiredVersions` names an extension version that does not exist yet. | Check `azd extension list --installed` and lower the floor. `azure.ai.agents` is at `1.0.0-beta.13`/`.14`. |
| `AcrImageNotFound` (404) | The build did not publish the tag. | Re-run `azd deploy` and read the build log. |
| Image runs on your laptop but not in Foundry | ARM image. | The platform requires `linux/amd64`. azd's remote ACR build produces that; a local build on Apple Silicon needs `docker build --platform linux/amd64`. |
| Version never reaches `active`; health probe never passes | Container is not answering the platform's probe. | The runtime contract requires listening on **8088** and returning 200 from **`GET /readiness`**. `azure.yaml` sets `T2PR_PORT: "8088"`; if the probe still fails, check `azd ai agent monitor` for a startup crash — a bad env var makes `config.py` exit with a message naming the variable. |
| `A ${VAR} value is empty` | Not set in the active `azd` environment. | `azd env set <VAR> <value>`, then `azd env get-values` to confirm. |
| Model deployment fails | Model or version not in the catalog for that region, or no quota. | Check the catalog for your region; check quota. Some tiers must request it. |

| **The agent version never reaches `active`, and `/readiness` never returns 200** | The version was deployed **before** `T2PR_ADO_PAT` existed in the azd environment. Environment variables are baked into the agent version at deploy time; `config.py` refuses to start without the PAT, so the version can never pass its probe. Re-running does not help unless something redeploys. | `./scripts/setup.sh --from 5` — steps 5 and 6 create the connection and the placeholder, and step 7 notices the deployed version's baked variables no longer match and redeploys. Check what a version was built with: `azd ai agent show --output json` → `definition.environment_variables`. |

### Invoking

| Symptom | Cause | Fix |
|---|---|---|
| `401 ... audience is incorrect (https://ai.azure.com)` | Token minted for `cognitiveservices.azure.com`. | `az account get-access-token --resource https://ai.azure.com`. |
| `401` from the agent endpoint with a correct audience | Caller has no data-plane role. | Assign `Foundry Agent Consumer` ([§5](#5-let-callers-invoke-the-agent)). |
| `400 Missing required query parameter: api-version` | The `?api-version=v1` was stripped. | Put it back on every gateway call, including the status GET. |
| `400 Invalid invocation ID format` | Polling with an id the gateway did not issue. | Poll with the `runId` the `POST` returned. The agent adopts the gateway's `x-agent-invocation-id` as its run id, so the body value and that header agree; a client that invents its own id, or reuses one from another deployment, gets this. |
| `403` from the agent endpoint | Caller has no data-plane role. | Assign `Foundry Agent Consumer`. Note that `Owner` and `Contributor` do **not** grant data-plane access. |
| `400 Extra inputs are not permitted` | Unknown field in the body. | The contract has no `harness`, `model` or `coder`, and the wire format is camelCase only — `work_item` is also an unknown field. |

### Runs

| `error.code` | What it usually means |
|---|---|
| `ado_forbidden` | Either the organization is not in `T2PR_ALLOWED_ORGS`, or the PAT is missing, expired, revoked, or lacks a scope. Check the allowlist first — it is the cheaper mistake. |
| `work_item_not_found` | Wrong id, or the PAT's account cannot see the area path, or `workItem.type` is not a type the project defines. |
| `repository_not_found` | Wrong repository name, or the PAT's account lacks `Read` on it. |
| `clone_failed` / `push_failed` | The PAT is missing **Code (Read & Write)**, or its account lacks `Contribute` / `Create branch` on the repository. |
| `pr_failed` | The PAT is missing **Pull Request (Read & Write)**, or its account lacks `Contribute to pull requests`. The remote branch has already been deleted, so retrying the same branch name is safe. |
| `harness_no_changes` | The model ran and decided nothing needed changing. Usually a thin work item — add acceptance criteria. |
| `timeout` | Raise `T2PR_MAX_RUN_MINUTES` in `azure.yaml` and redeploy, or split the ticket. |

### Harness

| Symptom | Cause | Fix |
|---|---|---|
| **`401 Principal does not have access to API/Operation` on the first real run** | **The agent identity has no model role. azd grants it none.** | `./scripts/setup.sh --only 7`, or the `az role assignment create` in [§5](#the-agents-own-model-access--azd-grants-it-nothing). **Wait 30–150 seconds before assuming the role is wrong** — assignments take that long to take effect. `Cognitive Services User` alone is verified sufficient; try `Cognitive Services OpenAI User` or `Foundry User` only if it still fails after the wait. |
| **`400 Missing required query parameter: api-version` from Claude Code** | **`ANTHROPIC_FOUNDRY_BASE_URL` was built from the *project* endpoint.** The request then reaches Foundry's generic project API rather than the Anthropic route, and complains about a parameter the correct route never needs. | It must be the **account** endpoint plus `/anthropic`: `https://{resource}.services.ai.azure.com/anthropic`. The harness derives that by stripping the `/api/projects/{name}` path off `FOUNDRY_PROJECT_ENDPOINT` — see `_anthropic_base_url` in `harness/claude_code.py`. |
| **`401` from the Codex harness** | **Expected.** Microsoft's Codex-on-Foundry page (ms.date 2026-09-03) states "Entra ID support is currently not available for Codex", and the agent will not use an API key. | Switch to `claude-code`: `azd env set T2PR_HARNESS claude-code && azd deploy`. **Read [§8.1](#81-what-switching-to-claude-code-changes) first — the switch removes the Codex sandbox.** Track [openai/codex#2024](https://github.com/openai/codex/issues/2024). |
| `401` from *either* harness partway through a long run | The inference token the agent minted at the start of the run expired; neither CLI can refresh a token supplied to it. The agent logs a `WARNING` naming the token's remaining lifetime and the run budget. | Lower `T2PR_MAX_RUN_MINUTES` so the budget fits inside an Entra access-token lifetime (typically an hour or more; the 20-minute default has ample margin). |
| `Failed to get token from azureADTokenProvider` from Claude Code | The default credential chain found nothing. | In Foundry this should not happen — the agent identity is injected. Locally, `az login`. |
| Claude Code picks the wrong model | Aliases (`sonnet`, `opus`, `haiku`) resolve to Claude Code's built-in Foundry defaults, not your deployments. | The harness already pins `ANTHROPIC_MODEL` and every `ANTHROPIC_DEFAULT_*_MODEL` to `T2PR_MODEL_DEPLOYMENT`. Confirm that value matches a real deployment name. |
| `bypassPermissions` refuses to start | Claude Code will not run in that mode as root. | `agent/Dockerfile` already runs as a non-root user. If you changed it, change it back. |

### 8.1 What switching to `claude-code` changes

`claude-code` is the default and the harness this guide recommends. It also **removes a
sandbox**, which is worth knowing:

- `codex exec` runs with `--sandbox workspace-write`. The CLI itself confines writes to
  the working tree.
- `claude --permission-mode bypassPermissions` has **no sandbox**. Generated code may read
  and write anything the container's user can, and run any command, without prompting.

The container is then the only boundary. That is a deliberate, accepted trade — the
container runs as a non-root user, holds no Azure DevOps credential and no means of
minting one, and its working directory is deleted at the end of every run — but it is a
real reduction in defence in depth, and it is why four pieces are load-bearing rather than
decorative:

1. **The separate harness uid** (`agent/Dockerfile`, `runner.py`, `launch.py`). The server
   is uid 10001; the runner and every CLI it spawns are uid 10002. This is the boundary —
   it is what makes `/proc/<server>/environ`, `/proc/<server>/mem`, the server's `$HOME`
   and a projected workload-identity token file all unreachable from a coding run.
2. **The ENTRYPOINT launcher** (`launch.py`), which scrubs the managed-identity plumbing
   out of the server's exec-time environment and `execve`s the runner rather than merely
   forking it.
3. **The environment allowlist** in `harness/__init__.py`, which decides what a harness
   subprocess may see at all.
4. **The isolated healthcheck** (`python -I` in `agent/Dockerfile`), so a probe cannot
   import a `usercustomize.py` a harness left behind.

See [SECURITY.md](../SECURITY.md), Credentials, for the findings each of these answers. If
you change the image, do not undo any of them — and note that the launcher must start as
root to drop the runner to its own uid; uid 10001 cannot become 10002.

### Where to look

```bash
azd ai agent monitor                       # live container logs
azd ai agent show --output table           # version, status, env, resources
azd ai agent doctor                        # local + remote health checks
```

Deeper traces land in the Application Insights resource `azd` provisioned:
**Investigate → Transaction search** or **Performance**. Reading them needs
`Monitoring Reader` on that resource — `Foundry User` is not enough.

---

## 9. Tearing it down

```bash
./scripts/teardown.sh
```

It reverses `setup.sh` step by step, from 9 back to 2, and asks before every destructive
action. Nothing is deleted on a bare `Enter`.

It works through the steps roughly in reverse, with one deliberate departure: the **hosted
agent is offered for deletion before** the PAT connection and the placeholder variable it
depends on. In strict reverse order, answering "no" to "delete the hosted agent" after
already deleting the connection left a live agent whose `${{connections...}}` no longer
resolved — running, billing, and failing every invocation with `ado_forbidden`. If you keep
the agent, both later steps now say so before touching anything it needs.

It only removes what `setup.sh` recorded creating. Step 2 onwards writes `T2PR_SETUP_*`
markers into the azd environment — `T2PR_SETUP_CREATED_PROJECT`, `T2PR_SETUP_CREATED_ACR`,
`T2PR_SETUP_CREATED_MODEL_DEPLOYMENT`, `T2PR_SETUP_CREATED_ENTRA_APP`,
`T2PR_SETUP_GRANTED_ACRPULL_ACCOUNT`, `T2PR_SETUP_GRANTED_ACRPULL_PROJECT`, the agent
principal id, the PAT's `authorizationId` — and teardown reads them. **A Foundry project
you connected to is never deleted**; a registry, model deployment, app registration or role
assignment that predates setup is left alone and says so.

Nothing is ever located by display name. Entra display names are not unique in a tenant, so
a name match identifies nothing — and `az ad app delete` is not reversible. Teardown deletes
the app registration only when `T2PR_SETUP_CREATED_ENTRA_APP` is `true` **and**
`T2PR_ENTRA_CLIENT_ID` names it. On the create side, setup asks before adopting a same-named
app it did not create, because configuring one rewrites its sign-in audience and replaces
its redirect URIs.

What it does, in order:

| | |
|---|---|
| 9 | Deletes the gitignored build outputs: `extension/deploy.local.json`, `extension/vss-extension.local.json`, `extension/dist/agent-config.json` and the `.vsix` in `extension/dist-vsix/`. Each carries your agent URL and Entra app registration. It asks before every one. |
| 8 | Deletes the Entra app registration — which deletes its service principal and the consent grant with it — **only if setup created it**. An app you already had, or one an administrator made for you, is left alone. |
| 7 | Clears the `T2PR_ADO_PAT` placeholder variable. |
| 6 | Deletes the `ado-secrets` Foundry connection, then **revokes the PAT** via the token lifecycle API. Deleting the connection alone does not revoke anything. |
| 5 | Removes all three candidate model roles from the agent identity, then deletes the hosted agent. |
| 4 | Removes each AcrPull assignment **only if setup granted it**; deletes the registry only if setup created it. A shared registry usually already carries AcrPull for these principals because something else deploys from it, and stripping that would break the other deployment. |
| 3 | Deletes the model deployment only if setup created it; then `azd down --force --purge` if setup created the project, otherwise offers to unbind it and reset `azure.yaml` to the create path. |
| 2 | Offers to delete `.azure/<env>/`. |

`azd down` on its own still works if you would rather not use the script — but it does not
revoke the PAT, does not delete the Entra app registration, and does not remove role
assignments on resources it did not create.

Clean up Azure DevOps separately: uninstall the extension from the organization, and if
you added any identity to **Organization settings → Users** by hand, remove it.

---

## Appendix A — what `setup.sh` does, and how to do it by hand

Run `./scripts/setup.sh --list` to see the steps, and read the script: it is commented
with the error each step prevents. This appendix is the same nine steps as commands, for
automating them differently or running them under a different identity.

### A1. Preflight

```bash
az version; azd version; python3 --version; git --version; curl --version
node --version; npm --version            # only step 9 (the extension .vsix) needs these
azd extension install microsoft.foundry
az login
azd config set auth.useAzCliAuth true    # azd reuses the az login; avoids a second sign-in
azd auth login --check-status

# Resource providers. Nothing in azd registers one, and a subscription that has never run
# a Foundry or azd workload commonly has Microsoft.CognitiveServices unregistered.
for NS in Microsoft.CognitiveServices Microsoft.ContainerRegistry \
          Microsoft.OperationalInsights Microsoft.MachineLearningServices; do
  az provider show -n "$NS" --query registrationState -o tsv
  az provider register -n "$NS" --wait     # idempotent; a minute or two each
done
```

`curl` is not optional — [A5](#a5-the-pat) mints the PAT with it and `teardown.sh` revokes
with it. It is absent from `python:*-slim` and from plenty of minimal CI images.

Skipping the provider registration is what produces `MissingSubscriptionRegistration` from
`azd provision`, which reads like a missing role or a bad region and is neither.

### A2. Environment and parameters

```bash
azd env new dev                # or: azd env select dev
azd env set AZURE_SUBSCRIPTION_ID "$(az account show --query id -o tsv)"
azd env set AZURE_LOCATION        swedencentral
azd env set T2PR_HARNESS          claude-code
azd env set T2PR_MODEL_DEPLOYMENT claude-opus-5
azd env set T2PR_ALLOWED_ORGS     contoso
```

### A3. Foundry project, and the model deployment

**Create:** leave `azure.yaml`'s `endpoint:` line commented out and run `azd provision`.

**Connect:** uncomment it, set it to your project endpoint, then:

```bash
azd ai project set "https://acct.services.ai.azure.com/api/projects/core"
azd env set AZURE_AI_PROJECT_ID \
  "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/acct/projects/core"
```

Either way, check what the subscription can actually deploy before asking for it. Quota is
per model, per SKU and per **region**, and `claude-opus-5` / version `2` / capacity `40`
were this deployment's answers, not universal ones:

```bash
# What this subscription is entitled to, in this region. The counter is named
# AIServices.<sku>.<model>, with a parallel `.Azure` counter carrying the consumption.
az cognitiveservices usage list -l "$LOCATION" \
  --query "[?contains(name.value,'claude-opus-5')].[name.value,limit,currentValue]" -o tsv

# What this account can actually offer, and which version is current.
az cognitiveservices account list-models -n "$ACCOUNT" -g "$RG" \
  --query "[?format=='Anthropic' && name=='claude-opus-5'].[version,isDefaultVersion]" -o tsv
```

A model the subscription has no entitlement to is either absent from the usage list or
present with `limit` `0`. `setup.sh` reads both of these and refuses to send a `PUT` it
knows will be rejected.

Then the model deployment for the `claude-code` harness is an ARM `PUT`, because
`azure.yaml` cannot carry `modelProviderData`:

```bash
cat > deployment.json <<'JSON'
{
  "sku": { "name": "GlobalStandard", "capacity": 40 },
  "properties": {
    "model": { "format": "Anthropic", "name": "claude-opus-5", "version": "2" },
    "modelProviderData": {
      "organizationName": "Contoso",
      "countryCode": "SE",
      "industry": "Technology"
    }
  }
}
JSON

az rest --method put \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT/deployments/claude-opus-5?api-version=2025-06-01" \
  --headers "Content-Type=application/json" --body @deployment.json
```

`az rest --method put` returns as soon as ARM **accepts** the request, so a `200` is not a
finished deployment. Poll before relying on it:

```bash
until [ "$(az cognitiveservices account deployment show -n "$ACCOUNT" -g "$RG" \
             --deployment-name claude-opus-5 --query properties.provisioningState -o tsv)" \
        = Succeeded ]; do sleep 10; done
```

Preflight-validate it first with `az deployment group validate` if you want to check the
Marketplace fields without creating anything.

### A4. Container registry, and AcrPull for both identities

```bash
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false
ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)

azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT    "$(az acr show -n "$ACR" --query loginServer -o tsv)"
azd env set AZURE_CONTAINER_REGISTRY_RESOURCE_ID "$ACR_ID"

ACCOUNT_MI=$(az cognitiveservices account show -n "$ACCOUNT" -g "$RG" --query identity.principalId -o tsv)
PROJECT_MI=$(az resource show --ids "$PROJECT_ARM_ID" --api-version 2025-06-01 --query identity.principalId -o tsv)

for MI in "$ACCOUNT_MI" "$PROJECT_MI"; do
  az role assignment create --assignee-object-id "$MI" \
    --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID"
done
```

Both. Granting only the account identity is what produces
`[ImageError] Container registry authentication failed`.

On the connect path, run `azd provision` once after setting those two variables. Step 3
never ran it — the project already existed — and this is what creates the project's
container-registry connection. Against an already-complete environment
`azd provision --preview` plans exactly one resource, that connection, so it is close to a
no-op when it has already been done.

### A5. The PAT

Mint one with the token lifecycle API, or create it in the Azure DevOps UI with the three
scopes from [§3.1](#31-the-three-scopes):

```bash
ADO_TOKEN=$(az account get-access-token \
  --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv)

curl -sS -X POST -H "Authorization: Bearer $ADO_TOKEN" -H 'Content-Type: application/json' \
  -d '{"displayName":"ticket-to-pr-agent","scope":"vso.work vso.code_write",
       "validTo":"2026-12-31T00:00:00Z","allOrgs":false}' \
  "https://vssps.dev.azure.com/$ORG/_apis/tokens/pats?api-version=7.1-preview.1"
```

The response's `patToken.token` is the secret; `patToken.authorizationId` is not, and is
what revokes it later. Store the token in the Foundry connection:

```bash
azd ai connection create ado-secrets --kind remote-tool \
  --target "https://dev.azure.com/$ORG" --auth-type custom-keys \
  --custom-key "pat=$PAT" --force
```

### A6. The placeholder

```bash
azd env set T2PR_ADO_PAT '${{connections.ado-secrets.credentials.pat}}'
```

### A7. First deploy, then the agent's model role

```bash
azd deploy
PRINCIPAL=$(azd ai agent show --output json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["instance_identity"]["principal_id"])')

az role assignment create --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role "Cognitive Services User" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"
```

### A8. The Entra SPA app for the extension

Five things must all be true. Each missing one fails differently.

```bash
APP_ID=$(az ad app create --display-name ticket-to-pr-hub \
           --sign-in-audience AzureADMultipleOrgs --query appId -o tsv)
OBJ_ID=$(az ad app show --id "$APP_ID" --query id -o tsv)

# SPA redirect + the delegated permission. `az ad app update` has no --spa-redirect-uris.
az rest --method patch --url "https://graph.microsoft.com/v1.0/applications/$OBJ_ID" \
  --headers "Content-Type=application/json" --body '{
    "signInAudience": "AzureADMultipleOrgs",
    "spa": { "redirectUris": ["https://dev.azure.com/_public/_MsalPopup"] },
    "requiredResourceAccess": [{
      "resourceAppId": "18a66f5f-dbdf-4c17-9dd7-1634712a9cbe",
      "resourceAccess": [{ "id": "1a7925b5-f871-417a-9b8b-303f9f29fa10", "type": "Scope" }]
    }]
  }'

# A service principal for the app, and one for the resource if the tenant lacks it.
az ad sp create --id "$APP_ID"
az ad sp create --id 18a66f5f-dbdf-4c17-9dd7-1634712a9cbe   # ok to fail if it exists

SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
RES_ID=$(az ad sp show --id 18a66f5f-dbdf-4c17-9dd7-1634712a9cbe --query id -o tsv)

az rest --method post --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" \
  --headers "Content-Type=application/json" \
  --body "{\"clientId\":\"$SP_ID\",\"consentType\":\"AllPrincipals\",
           \"resourceId\":\"$RES_ID\",\"scope\":\"user_impersonation\"}"
```

| Piece | Why | What breaks without it |
|---|---|---|
| SPA redirect `https://dev.azure.com/_public/_MsalPopup` | The redirect the Azure DevOps nested-app-authentication broker uses. The page must not set `redirectUri` itself. | MSAL cannot complete the popup flow. |
| **No client secret** | The `.vsix` is downloadable by anyone who can install it. A secret in it is a published secret. MSAL runs as a public client with PKCE. | A leaked credential. |
| `--sign-in-audience AzureADMultipleOrgs` | Single-tenant fails under nested app auth with **`AADSTS50194`**: the Azure DevOps host frame brokers the token on `/common` and ignores the child app's authority. | `AADSTS50194` on every sign-in, even for users in your own tenant. |
| `user_impersonation` on resource app `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe` (Azure Machine Learning Services), scope id `1a7925b5-f871-417a-9b8b-303f9f29fa10` | The delegated permission behind the Foundry data plane. | There is no permission to consent to. |
| **A service principal** *and* an `oauth2PermissionGrant` with `consentType: AllPrincipals` | MSAL asks for `.default`, and `.default` returns only permissions **already consented**. With no service principal there is nothing to consent to; with no grant `.default` returns nothing and the token has no usable scope. | A token that looks fine and gets a 401 from the agent. |

`az ad app create` and `az ad sp create` need Application Administrator; the
`oauth2PermissionGrant` needs Privileged Role Administrator or Cloud Application
Administrator. If you cannot do it yourself, `setup.sh` prints exactly what to hand an
administrator, and accepts the resulting client id with
`azd env set T2PR_SETUP_ENTRA_CLIENT_ID <appId>`.

### A9. Extension configuration and the `.vsix`

**`setup.sh` runs this one for you.** Step 9 asks for the Marketplace publisher id and the
repository URL, records both in the azd environment, and then runs `npm ci`,
`npm run configure` and `npm run package` itself, printing the path of the `.vsix` it
produced. Nothing in that chain needs a credential the script does not already hold. It
stops before the upload, which does — see [extension.md](extension.md#4-install-privately).

It skips the build, says why in one line and prints the commands below instead when the
publisher id or the repository URL is still blank (both are required: `configure.mjs`
refuses to write a manifest that still carries a `REPLACE-WITH-` placeholder), or when the
machine has no `node`/`npm`. A skipped build is never fatal — the agent is deployed and
answering by then.

The extension configures **itself** from the azd environment. `extension/scripts/configure.mjs`
resolves each value from a flag, then a process environment variable, then
`azd env get-values`:

| Value | azd key `setup.sh` writes |
|---|---|
| Agent Application URL | `AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` (azd writes it; the `/invocations` suffix and query string are stripped for you) |
| Entra SPA client id | `T2PR_ENTRA_CLIENT_ID` |
| Entra tenant id | `T2PR_ENTRA_TENANT_ID` |
| Marketplace publisher id | `T2PR_EXT_PUBLISHER` |
| Repository/Support URL | `T2PR_EXT_REPO_URL` |

So the by-hand equivalent of step 9 needs no arguments beyond the two answers, and none at
all once `setup.sh` has recorded them:

```bash
cd extension
npm ci
npm run configure -- --publisher <id> --repo-url https://github.com/<org>/ticket-to-pr-agent
npm run package            # -> dist-vsix/<publisher>.ticket-to-pr-*.vsix
```

Both generated files are gitignored, and so is `dist/` and `dist-vsix/`.
`extension/src/agent-config.json` is a tracked, deliberately **empty** template — nothing
tenant-specific is ever written into a tracked file, which is what keeps this repository a
template. After a full `npm run package`, whether you ran it or `setup.sh` did,
`git status` is still clean.

`npm run configure --print` shows the resolved configuration without writing anything;
use it to check that `setup.sh` recorded what the extension needs. A partially pinned
configuration is a hard error rather than a silent fallback to per-user settings.

Full walkthrough, including creating the publisher and installing privately, in
[extension.md](extension.md).

---

## Appendix B — facts this deployment established

Each of these was found by something failing. They are recorded with the error so the next
person can search for it.

| Fact | The error you get otherwise |
|---|---|
| The Foundry model token audience is **`https://ai.azure.com/.default`**, not `cognitiveservices.azure.com`. | `401 ... audience is incorrect (https://ai.azure.com)` |
| Claude Code's `ANTHROPIC_FOUNDRY_BASE_URL` is the **account** endpoint plus `/anthropic`, not the project endpoint plus `/anthropic`. | `400 Missing required query parameter: api-version` — about a parameter the correct route never needs. |
| Every gateway call needs **`?api-version=v1`**, including the status GET. | `400 Missing required query parameter: api-version` |
| The run id is the gateway's own invocation id. The agent **adopts** `x-agent-invocation-id` as its `runId`, so the body and the header carry the same value and either can be polled with. | Inventing a client-side id gives `400 Invalid invocation ID format`, rejected by the gateway before it reaches the container. |
| `azd deploy` against an **existing** project needs `AZURE_AI_PROJECT_ID` and both `AZURE_CONTAINER_REGISTRY_ENDPOINT` and `AZURE_CONTAINER_REGISTRY_RESOURCE_ID` in the azd environment. | `AZURE_AI_PROJECT_ID does not identify the project configured by the azure.yaml endpoint`; `reuse-connect requires both container registry endpoint and resource ID` |
| The agent's instance identity gets **no** role assignments from azd. | `401 Principal does not have access to API/Operation` on the first real run. |
| A hosted agent's environment variables are **baked into the agent version at deploy time** (`azd ai agent show` → `definition.environment_variables`). Nothing re-reads the azd environment afterwards. | A version deployed before `T2PR_ADO_PAT` exists never passes `/readiness` and never reaches `active`, and re-running the deploy step is the only repair. This is why `setup.sh` does the PAT before the deploy. |
| Nothing in azd registers a **resource provider**. | `MissingSubscriptionRegistration: The subscription is not registered to use namespace 'Microsoft.CognitiveServices'` from `azd provision` — which reads like a missing Contributor role or an unsupported region and is neither. |
| Model quota counters are named **`AIServices.<sku>.<model>`**, with a parallel `.Azure` counter carrying the consumption booked against the Azure route. A model the subscription is not entitled to is absent, or present with `limit` `0`. Read 2026-09-10: `AIServices.GlobalStandard.claude-opus-5` limit 40 current 0, `…claude-opus-5.Azure` limit 40 current 40 — one 40k deployment exists, so reading either counter alone gets the headroom wrong. | `InsufficientQuota` from the ARM `PUT`, after the questions have already been answered. |
| `az rest --method put` on a model deployment returns as soon as ARM **accepts** the request. | An async accept looks exactly like success, and the next step deploys an agent pointed at a model that is not there yet. Poll `properties.provisioningState`. |
| `ENABLE_HOSTED_AGENTS=true` and `ENABLE_CAPABILITY_HOST=false` are written into the azd environment by the `azure.ai.agents` extension's own `preprovision`/`predeploy` hooks, hard-coded, whenever the project's agent definition is a hosted agent. They are not prompted, and hand-editing them does not stick — the hook rewrites both on every `azd provision` and `azd deploy`. **Under `infra.provider: microsoft.foundry` they are inert**: the `microsoft.foundry` provider lives in `azure.ai.projects`, whose embedded `main.bicep` has no `enableHostedAgents` or `enableCapabilityHost` parameter and no `capabilityHosts` resource — the ACR is gated by `includeAcr` instead. They matter only for agent templates that ship their own `infra/`. Verified against extension `azure.ai.agents` 1.0.0-beta.13, 2026-09-10. | Nothing. This is recorded because they appear in a working `.env` and in no repository file, which looks like a missing step and is not. `setup.sh` deliberately does **not** set them. |
| **AcrPull is needed on both** the Cognitive Services account identity and the project identity. azd's Bicep grants only the project. | `[ImageError] Container registry authentication failed` |
| `azure.yaml` cannot contain `${{connections...}}` directly. | `unable to parse variable name` |
| `${VAR}` is **not** substituted inside an `azure.ai.project` service — only in the agent service's `env:` map and under `network:`. | A model deployment planned with the literal name `${T2PR_MODEL_DEPLOYMENT}`. |
| `azure.yaml` cannot create an **Anthropic** model deployment. | `InvalidModelProviderData: ModelProviderData is required for Anthropic model deployments but was not provided.` |
| `azd provision` needs `infra.provider: microsoft.foundry`; this repository ships no `infra/`. | `Could not find a part of the path '<repo>/infra/main.bicep'` |
| The extension's Entra app must be **multi-tenant** under nested app auth. | `AADSTS50194` |
| `.default` returns only already-consented permissions, so the app needs a service principal **and** an `AllPrincipals` grant. | A token that looks valid and gets a 401. |
| `requiredVersions` must name an extension version that exists. | `azd deploy` fails its constraint check before doing any work. |
