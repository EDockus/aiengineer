# ticket-to-pr-agent

<!--
Owner-agnostic badge: a relative link in a README resolves against the repository it is
being viewed in, so this keeps working under any account, organisation or fork.
-->
[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[MIT](LICENSE) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

An open-source [Microsoft Foundry](https://ai.azure.com) hosted agent that turns an Azure
DevOps work item into a branch and a pull request. Point it at a ticket and a repository;
it fetches the work item, clones the repo, runs a coding harness (the Codex CLI or the
Claude Code CLI) against a model deployed in the same Foundry project, commits, pushes and
opens a draft pull request linked back to the ticket. Harness and model are set at deploy
time, not per request, so a whole organization can share one agent without arguing about
either. It never merges.

**Two auth paths, and they are not the same.** To the **model** there is no key: the agent
uses its own Entra agent identity. To **Azure DevOps** it uses a Personal Access Token
(`T2PR_ADO_PAT`). That half is forced, not chosen — Azure DevOps will not entitle a Foundry
agent identity (`VS403283`), and that identity cannot federate into one Azure DevOps does
accept (`AADSTS700231`). The routes tested and the errors each returned are the decision
record in
[docs/deploy.md §3](docs/deploy.md#why-not-the-agent-identity--the-decision-record). The
PAT is a secret: never logged, never in a pull request body or commit message, never passed
to the harness. All Azure DevOps auth sits behind one token provider so it can be swapped the day
the platform catches up.

**Status.** Early. The agent and the Azure DevOps extension are deployed and verified end
to end against a live Foundry project and a live Azure DevOps organization — a real work
item becomes a real draft pull request. What has *not* been exercised is
`./scripts/setup.sh` run start to finish on a **clean subscription and a clean Azure DevOps
organization**. Two of its nine steps have run against real Azure — the deploy and the
extension build. The other seven have only ever run against stubbed `az`/`azd`, end to end
on Linux, where the step order, preconditions, quota checks and resource-provider checks
all hold. No fresh subscription has created a resource with it. Expect to read
[docs/deploy.md](docs/deploy.md) when something in it surprises you, and please open an
issue when it does.

```
   ┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
   │  ADO extension    │   │  Function App     │   │  your own client  │
   │  (hub page, MSAL) │   │  (service hook)   │   │  (curl, CI, bot)  │
   └─────────┬─────────┘   └─────────┬─────────┘   └─────────┬─────────┘
             └───────────────────────┼───────────────────────┘
                                     │  POST /invocations  → 202 { runId }
                                     │  GET  /invocations/{runId}
                                     │  Authorization: Bearer <Entra token>
        ╔════════════════════════════▼════════════════════════════════════╗
        ║                    Microsoft Foundry project                    ║
        ║                                                                 ║
        ║   agent endpoint ──► ┌──────────────────────────────────────┐   ║
        ║   (Entra auth,       │  hosted agent  (per-session sandbox) │   ║
        ║    invocations)      │                                      │   ║
        ║                      │   main.py    run store, phases       │   ║
        ║                      │   run.py     orchestration           │   ║
        ║                      │   ado.py ──┐  git_ops.py ──┐         │   ║
        ║                      │   harness/ ─┼──────────────┼── /work │   ║
        ║                      └─────────────┼──────────────┼─────────┘   ║
        ║                                    │              │             ║
        ║   model deployment ◄───────────────┘              │             ║
        ║   (GPT-6 / Claude)   Entra token, agent identity  │             ║
        ╚═══════════════════════════════════════════════════╪═════════════╝
                                                            │
                                 Personal Access Token, T2PR_ADO_PAT
                                 (not the agent identity — deploy.md §3)
                                                            │
                                     ┌──────────────────────▼────────────┐
                                     │        Azure DevOps               │
                                     │  work items · repos · pull reqs   │
                                     └───────────────────────────────────┘
```

## Quick start

```bash
git clone https://github.com/EDockus/aiengineer.git
cd aiengineer
./scripts/setup.sh
```

That is the deployment. The script asks for the harness, the model deployment and your
Azure DevOps organization, then does the nine things `azd up` does not — and finishes by
building the Azure DevOps extension package.

The PAT comes before the deploy on purpose: a hosted agent's environment variables are
baked into the agent version, so a version deployed before the token exists never starts
and nothing later repairs it. It is safe to re-run — every step checks the world before it
changes it, and the deploy step redeploys by itself when the configuration it was built
with has moved on.

### What the script does, and what you still do yourself

`setup.sh` does everything reachable with the credentials it already holds: your Azure
login, and one Azure DevOps PAT. Four things sit outside that, and each is left out for a
reason rather than by omission.

**The script does this, in this order:**

| | |
|---|---|
| 1 | Checks tools and versions, signs `az` and `azd` in, and registers the four **resource providers** a fresh subscription lacks. Nothing in azd registers one. |
| 2 | Creates the azd environment and sets the three deploy-time parameters — harness, model deployment, allowed organizations. |
| 3 | Creates the **Foundry project**, or connects to one you already have, then checks quota and creates the **model deployment**. |
| 4 | Creates the **container registry** and grants **`AcrPull` to both** identities that pull from it. azd's own Bicep grants one. |
| 5 | **Mints the Azure DevOps PAT** for you (or takes yours) and stores it as a Foundry **connection**. |
| 6 | Writes the `${{connections…}}` **indirection** that `azure.yaml` cannot express directly. |
| 7 | Deploys, then grants the agent's own instance identity a **model role**. azd grants it none, so without this every run 401s. |
| 8 | Registers the **Entra app** the extension signs in with: SPA redirect URI, multi-tenant audience, `user_impersonation` on Azure Machine Learning Services, a service principal, and the admin consent grant. |
| 9 | Records the Marketplace publisher id and repository URL, then **builds the extension `.vsix`** — `npm ci`, `npm run configure`, `npm run package` — and prints the path. |

**You do this:**

| | Why it is not automated |
|---|---|
| Create a Marketplace **publisher** | An account action at <https://marketplace.visualstudio.com/manage>. There is no API for it. |
| **Upload** the `.vsix` and **share** it with your organization | Publishing needs a Marketplace PAT — a second credential, and this script deliberately takes exactly one secret. Budget ~100 seconds for Marketplace verification: until it clears, the extension's page 404s and the management API answers `ExtensionDoesNotExistException`, which reads exactly like a failed upload. |
| **Install** the shared extension into the Azure DevOps organization | An organization action, and it needs the **Manage extensions** permission — which is also the access control on the settings pinned into the package. |
| Create the **Azure DevOps PAT**, if you decline step 5's offer to mint one | Only if you say no. The script can create it against your `az` login and never prints it. |

Step 9 skips the build and prints the four commands instead when the publisher id or the
repository URL is still blank, or the machine has no `node`/`npm` — Node is a warning in
step 1, not a requirement. A skipped extension build never fails the run: the agent is
deployed and answering by the time step 9 starts.

"Automated" here means the script issues the calls, not that a clean subscription has run
them — see **Status** above.

Then smoke-test it:

```bash
BASE=$(azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT)   # already has ?api-version=v1
TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)

BODY=$(curl -s -D headers.txt -X POST "$BASE" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"version":"1","organization":"contoso","project":"Platform","workItem":{"type":"Bug","id":4123},"repository":"payments-api","branch":"bugfix/4123-null-ref"}')
```

Then poll **the id that POST handed back**, which is why the headers are kept as well as
the body:

```bash
# The gateway stamps the id it will accept on the response. Locally there is no gateway
# and no header, and the body's runId is the id. Take whichever is there.
RUN=$(awk 'tolower($1)=="x-agent-invocation-id:"{print $2}' headers.txt | tr -d '\r')
RUN=${RUN:-$(printf '%s' "$BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runId"])')}

curl -s -H "Authorization: Bearer $TOKEN" "${BASE%%\?*}/$RUN?api-version=v1"
```

Polling with an id the gateway did not issue gets you `400 Invalid invocation ID format`;
[docs/deploy.md](docs/deploy.md) has that in its troubleshooting table. The full
walkthrough, the manual equivalent of every step, and the errors each one prevents are
there too; [docs/contract.md](docs/contract.md) is the interface itself.
`./scripts/teardown.sh` reverses the deployment.

To try the whole orchestration on a laptop with **no Azure subscription and no Azure DevOps
organization**, see [agent/README.md](agent/README.md#3-run-it-locally-end-to-end): a local
ADO stub plus a fake harness runs fetch → clone → branch → code → commit → push → pull
request against a real bare git repository on disk.

## Which harness?

**Use `claude-code`.** Its Microsoft Foundry path authenticates with Microsoft Entra ID and
is documented and supported. Microsoft's Codex-on-Foundry page currently states *"Entra ID
support is currently not available for Codex"*, and this project will not fall back to an
API key, so the Codex harness is expected to return 401 against Foundry until that ships.
The reasoning and the sources are in
[docs/configuration.md §4](docs/configuration.md#4-harness-configuration--what-was-verified).

## Documentation

| | |
|---|---|
| [docs/contract.md](docs/contract.md) | The published request/response interface, with curl. |
| [docs/deploy.md](docs/deploy.md) | `scripts/setup.sh`, the nine things `azd up` does not do, the PAT, caller roles, cost, local dev, troubleshooting. |
| [docs/configuration.md](docs/configuration.md) | Every environment variable, and what was verified about each CLI. |
| [docs/architecture.md](docs/architecture.md) | Runtime and protocol, the run sequence, components, design decisions, trust boundaries. |
| [docs/extension.md](docs/extension.md) | Packaging and installing the Azure DevOps extension. |
| [agent/README.md](agent/README.md) | Building and running the agent locally. |
| [examples/function-app/](examples/function-app/) | Sample service-hook receiver. |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) | |

## What it does not do

No multi-repo or multi-ticket runs — one work item, one repository, one branch, one pull
request per invocation. No auto-merge; the agent raises the pull request and stops. No
GitHub or other SCMs in v1. No web hosting of its own.

## License

MIT. See [LICENSE](LICENSE).
