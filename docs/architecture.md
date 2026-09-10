# Architecture

What the agent is, what each piece is responsible for, and the guarantees the rest of the
repository is written to keep. [contract.md](contract.md) is authoritative for anything a
caller can see; this page is authoritative for everything behind it.

## 1. The whole system

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

Two things carry all the weight:

- **Two auth paths, and they are not the same.** The hosted agent gets a dedicated Entra
  agent identity at deploy time and calls the **model** with it — no key on that path.
  **Azure DevOps** takes a Personal Access Token (`T2PR_ADO_PAT`) instead, because Azure
  DevOps will not entitle a Foundry agent identity (`VS403283`) and that identity cannot
  federate into one it does accept (`AADSTS700231`). The routes tested are in
  [deploy.md → Why not the agent identity](deploy.md#why-not-the-agent-identity--the-decision-record).
  The PAT arrives at runtime as an environment variable: nothing is in the image, and it
  never reaches the harness.
- **Harness and model are deploy-time configuration.** The request contract has no way to
  ask for a different coder or a different model. That is what makes the agent safe to
  expose to a whole organization.

## 2. Runtime and protocol

The agent is a **Microsoft Foundry hosted agent**: a container the platform runs in a
per-session sandbox, reached through the project's agent endpoint. Python 3.14, FastAPI,
built from `agent/`.

| | |
|---|---|
| Protocol | **Invocations, version 1.0.0.** Structured JSON request, structured JSON response. Declared in [`azure.yaml`](../azure.yaml) under `protocols:`. Version 1.0.0 is the container protocol this image implements: on 1.0.0 the platform propagates caller identity, which is what the agent assumes. Raising it to 2.0.0 means first teaching the container to forward the per-request `x-agent-foundry-call-id` header on outbound Foundry calls. |
| Endpoint auth | `authorizationSchemes: Entra`. The gateway validates the caller's token and does **not** forward the `Authorization` header to the container — see [§6](#6-trust-boundaries). |
| Background mode | On. `POST /invocations` returns `202` with a run id immediately and the run continues in the background; the caller polls. A coding run takes minutes, far longer than any sensible HTTP timeout. |
| Session idle timeout | 30 minutes (`sessionConfiguration.idleTimeoutSeconds: 1800` in `azure.yaml`; the platform's valid range is 120–3600). |
| Port | **8088.** The hosted-agent runtime contract requires it; the image's own default is 8000, so `azure.yaml` sets `T2PR_PORT: "8088"`. Deploy the image by any other route and you must set it yourself, or the platform's health probe never connects. |
| Routes | `POST /invocations`, `GET /invocations/{runId}`, `GET /healthz`, `GET /readiness`. Defined in [contract.md §1](contract.md#1-endpoints). `/healthz` returns 200 once configuration validation passes; `/readiness` is the platform's own probe and answers from the same handler — without it an agent version never reaches `active`. |

**`FOUNDRY_PROJECT_ENDPOINT` is injected by the platform and must never be declared in
`azure.yaml`.** The platform reserves the `FOUNDRY_` and `AGENT_` prefixes, and declaring
one shadows the platform value. The agent reads it straight from the environment
(`harness/__init__.py: foundry_endpoint()`). See
[configuration.md §2](configuration.md#2-variables-the-platform-injects--do-not-set-these).

## 3. A run, step by step

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant A as Agent (run.py)
    participant D as Azure DevOps
    participant W as /work (temp dir)
    participant H as Harness CLI
    participant M as Foundry model

    C->>A: POST /invocations
    A-->>C: 202 { runId, status: queued }
    Note over A: phase = fetching_work_item
    A->>A: organization in T2PR_ALLOWED_ORGS? else ado_forbidden
    A->>D: GET work item (title, description, AC, repro, tags, links)
    A->>D: GET repository, base branch; assert target branch absent
    Note over A: phase = cloning
    A->>W: git clone --depth 50 (PAT as HTTP Basic, via GIT_CONFIG_* only)
    A->>W: git checkout -b <branch>
    A->>W: strip .git/config credential helpers
    Note over A: phase = coding
    A->>H: run CLI in /work, prompt on stdin, allow-listed env only
    H->>M: inference with the agent identity
    H-->>A: exit code + final summary
    A->>W: git status; no changes → harness_no_changes<br/>too many files → harness_failed
    Note over A: phase = pushing
    A->>W: git commit "<type> #<id>: <title>"
    A->>D: git push
    Note over A: phase = creating_pr
    A->>D: POST pull request (draft, reviewers, "Fixes AB#<id>")
    A->>W: delete temp dir
    C->>A: GET /invocations/{runId}
    A-->>C: succeeded + { pullRequestId, pullRequestUrl, ... }
```

The same sequence as `run.py` executes it, with the phase reported in status and the
contract error code each step fails with:

| # | Step | Phase | Fails with |
|---|---|---|---|
| 1 | Validate the request; check `organization` is in `T2PR_ALLOWED_ORGS`. | — | `ado_forbidden` |
| 2 | Fetch the work item: title, description, acceptance criteria, repro steps, tags, linked work item titles. | `fetching_work_item` | `work_item_not_found` |
| 3 | Resolve the repository and base branch; assert the target branch does not already exist. | `fetching_work_item` | `repository_not_found`, `branch_exists` |
| 4 | Shallow-clone the base branch (`--depth 50`, single branch) into a per-run temp directory, with the Azure DevOps credential passed to `git` through `GIT_CONFIG_*` only. | `cloning` | `clone_failed` |
| 5 | Create and check out the target branch, then strip `.git/config` credential helpers. | `cloning` | `clone_failed` |
| 6 | Build the prompt ([§4.3](#43-prompt-construction)) and run the configured harness in the repository directory under the `T2PR_MAX_RUN_MINUTES` budget, capturing stdout and stderr to the run log. | `coding` | `harness_failed`, `timeout` |
| 7 | `git status`. No changes at all is a failure; more files changed than `T2PR_MAX_FILES_CHANGED` is a failure. | `coding` | `harness_no_changes`, `harness_failed` |
| 8 | Commit as `<type> #<id>: <title>` with the harness summary in the body, then push. | `pushing` | `push_failed` |
| 9 | Create the pull request: same title, a body with a summary, a "What changed" list, a "How to test" section, and the line `Fixes AB#<id>` so Azure DevOps links the work item. Draft and reviewers per configuration. **Every** way this call can fail is `pr_failed`, not only a 404. | `creating_pr` | `pr_failed` |
| 10 | Delete the temp directory and report `succeeded` with the result block. | `done` | — |

**No dangling remote branches.** Nothing is pushed until the harness has succeeded, and if
the pull request cannot be created after a push, the agent deletes the remote branch before
reporting `pr_failed`. There is one deliberate exception: when creation failed *ambiguously*
— a timeout, where Azure DevOps may have created the pull request anyway — the agent looks
for one on the branch first and leaves the branch alone if it finds one, because deleting
the source branch of a live pull request is the worse outcome. See
[../SECURITY.md](../SECURITY.md).

The temp directory is per-run and is deleted on the failure path too. Clones never
accumulate in `/work`.

## 4. Components

### 4.1 `agent/` — the hosted agent

| File | Responsibility |
|---|---|
| `main.py` | The Invocations server. `POST /invocations` validates and returns `202` immediately; the run executes in the background; `GET /invocations/{runId}` reports status; `GET /healthz` and `GET /readiness` both report health, the latter required by the Foundry runtime contract. |
| `config.py` | `Settings.from_env`. Reads and validates every variable at startup and refuses to start on anything it does not understand. Owns the dev-mode gate. |
| `models.py` | The pydantic wire contract. `extra="forbid"` and camelCase-only aliases are what turn an unknown field into a `400`. Also the `ErrorCode` enum and `RunFailure`. |
| `ado.py` | Azure DevOps REST client. Work items, work item types, repositories, refs, pull requests. Also the single Azure DevOps token provider: `T2PR_ADO_PAT` presented as HTTP `Basic` ([deploy.md §3](deploy.md#3-azure-devops-access-create-a-pat)). Everything that talks to Azure DevOps goes through it, so swapping the credential when the platform allows it is a one-file change. |
| `git_ops.py` | Clone, branch, commit, push, and credential hygiene — including stripping `.git/config` credential helpers before a harness runs. |
| `prompt.py` | Builds the coding prompt from the work item, and the commit message and pull request body. Strips HTML from rich-text fields and truncates each to a cap. |
| `harness/` | The `Harness` protocol, the factory, and `codex.py`, `claude_code.py`, `fake.py`. `clean_env` is the single place that decides what a harness subprocess may see. |
| `launch.py` | The container ENTRYPOINT. Starts as root, forks the runner and drops it to uid 10002, drops itself to 10001, then `execve`s the server with the managed-identity plumbing removed from its exec-time environment. Nothing is root once either process begins work. |
| `runner.py` | The unprivileged harness runner (uid 10002). Every coding CLI is spawned here, never by the server, so a prompt-injected harness cannot read the server's `/proc` entries or write its `$HOME`. Server and runner talk over an inherited `AF_UNIX` socketpair. |
| `run.py` | Orchestration and the in-memory run store. Maps each failure to its contract error code at the point it happens. |

### 4.2 The `Harness` protocol and factory

A harness is the thing that actually edits the working tree. It gets the repository files
and model access, and nothing else.

```python
class Harness(Protocol):
    name: str
    def preflight(self, cfg: Settings) -> None: ...   # CLI present, model endpoint configured
    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult: ...

@dataclass
class HarnessResult:
    exit_code: int
    summary: str          # the harness's own description of what it did, for the PR body
    stdout: str
    stderr: str
```

`get_harness(name)` is the factory. It raises `ConfigError` for an unknown value, and it is
called during startup validation so a bad `T2PR_HARNESS` is a refusal to start, not a
failure on the first request.

Two harnesses ship, plus one for tests:

- **`codex.py`** — the Codex CLI, non-interactively in the repository directory, approvals
  disabled, model set to `T2PR_MODEL_DEPLOYMENT`, endpoint and auth pointed at the Foundry
  project through environment variables.
- **`claude_code.py`** — Claude Code in print mode, permissions set to allow file edits and
  shell without prompting, same model and endpoint wiring.
- **`fake.py`** — writes one deterministic file and returns a canned summary. Dev and test
  only; `config.py` refuses to start with `T2PR_HARNESS=fake` outside dev mode.

The exact flags and environment each CLI is invoked with — and the documentation each claim
was checked against — are in
[configuration.md §4](configuration.md#4-harness-configuration--what-was-verified). Do not
guess a flag; look it up and record what you looked up.

Rules that hold for every harness, present and future:

- **Never pass Azure DevOps credentials into the harness environment.** `clean_env` builds
  the subprocess environment from an **allowlist** (`ENV_ALLOWLIST`), not by filtering
  `os.environ`, so a new `T2PR_*` setting cannot leak into a coding run by default.
- **Strip `.git/config` credential helpers** before invoking the harness.
- **The harness gets a pre-scoped token, never the means to mint one.** The managed-identity
  variables are deliberately absent from the allowlist: the identity endpoint issues a token
  for whatever audience the caller names, so a work-item description could otherwise talk a
  harness into minting an Azure DevOps token. The parent process mints one inference token
  per run and passes its value.
- **The prompt goes on stdin, never on argv.** It is untrusted work-item text, it can be
  tens of kilobytes, and argv is world-readable on the host.
- **The harness does not commit.** `run.py` commits, after inspecting the diff.
- Return the CLI's own final summary as `HarnessResult.summary`.

[CONTRIBUTING.md → Adding a harness](../CONTRIBUTING.md#adding-a-harness) is the procedure
for writing a new one.

### 4.3 Prompt construction

`prompt.py` fills one template from the work item:

```
You are implementing Azure DevOps work item #{id} ({type}) in this repository.

Title: {title}

Description:
{description}

Acceptance criteria:
{acceptance_criteria}

Repro steps:
{repro_steps}

Rules:
- Make the smallest change that fully satisfies the work item.
- Follow the repository's existing conventions, linters and test patterns.
- Add or update tests where the repository has them.
- Do not modify CI configuration, secrets, or infrastructure files unless the work item
  explicitly asks.
- Do not create commits; the caller will commit.
- When finished, print a short summary: what changed, how to test, anything left open.
```

An optional "Related work items" block is added when the work item has links, and omitted
entirely when it does not.

- **HTML is stripped** from Azure DevOps rich-text fields (`ado.strip_html`). The result is
  plain text, and entity references are resolved last so the text says what the field said.
- **Every field is capped at 8,000 characters** (`FIELD_CAP`) and the truncation is stated
  in-band, so the model knows it is not seeing everything.
- The same module builds the commit message and the pull request body. Harness output
  reaching a pull request description is redacted, capped and fenced in a code block one
  backtick wider than anything inside it — it is model output shaped by text anyone who can
  edit a ticket controls, sitting next to a heading a reviewer is meant to trust.

### 4.4 The container image

`agent/Dockerfile`:

- Base `python:3.14-slim`. Installs `git`, a pinned Node.js LTS tarball, the two coding
  CLIs (both npm packages) at pinned versions, and the Python package.
- **No credentials and no `.npmrc`.** Both CLIs come from the public registry, and every
  credential the agent uses is fetched at runtime. Nothing is baked into a layer.
- **Nothing runs as root.** The ENTRYPOINT starts as root only for as long as it takes to
  give the privilege away — it forks the runner and drops it to uid 10002, then drops itself
  to uid 10001 before `execve`ing the server. This is a correctness requirement as well as
  hardening: Claude Code refuses to run `bypassPermissions` as root.
- `/work` is writable for clones, `0750 agent:agent`.
- Both CLI versions are printed into the log at startup.
- Resources in `azure.yaml`: cpu `1`, memory `2Gi` — one of the three supported sandbox
  sizes, and coding runs are memory-hungry.

### 4.5 `extension/` — the Azure DevOps hub page

A static page inside an ADO extension package. Lists work items and repositories with the
ADO extension SDK, gets an Entra token for the agent with MSAL, POSTs the contract body,
polls every 5 seconds, and links to the pull request. No backend of its own. See
[extension.md](extension.md).

### 4.6 `examples/function-app/` — the sample webhook receiver

Shows the contract driven by something that is not the extension: an ADO service hook
fires on work item creation, the function filters on the `agent` tag, builds the contract
body and starts a run; a timer function polls and comments the pull request link back onto
the work item. See its [README](../examples/function-app/README.md).

### 4.7 `azure.yaml` — the deployment

One `azure.ai.project` service and one `azure.ai.agent` service built from `agent/`.
There is no `infra/` directory: `infra.provider: microsoft.foundry` hands provisioning to
the `azure.ai.projects` azd extension, which synthesises the Bicep itself, and everything
the agent needs — protocol, resources, session timeout, environment — is expressible in
`azure.yaml`.

Two things are deliberately *not*. The `ai-project` service's `endpoint:` line is the
create-or-connect switch and must be a literal, because `azd` performs no `${VAR}`
substitution inside that service; and the model deployment is created by
`scripts/setup.sh` rather than declared there, because ARM refuses Anthropic deployments
without a `modelProviderData` block that `azure.yaml`'s schema cannot express. Both are
recorded with their errors in
[deploy.md → What `azure.yaml` declares](deploy.md#what-azureyaml-declares).

`scripts/setup.sh` wraps `azd` with the nine steps it does not cover, and
`scripts/teardown.sh` reverses them. See [deploy.md](deploy.md).

## 5. Design decisions worth knowing

**Asynchronous by contract, not by platform.** A coding run takes minutes, far longer than
any sensible HTTP timeout, so `POST` returns a run id and the caller polls. The Invocations
protocol deliberately does not prescribe a status contract — it passes your payload through
untouched — so the polling shape in [contract.md](contract.md) is the agent's own.

**The run store is in memory.** Runs live in the process. The platform keeps a session's
sandbox alive across the idle timeout (30 minutes, `sessionConfiguration.idleTimeoutSeconds`)
and persists `$HOME`, but a run id does not survive a container replacement. Callers should
treat a `404` on a run id as "the run is gone", not "the run failed". If you need runs to
outlive the process, that is where to add durable storage.

**The harness environment is an allowlist, not a filter.** `harness/__init__.py` builds the
subprocess environment by naming what is permitted (`ENV_ALLOWLIST`) rather than by
removing what is not. A new `T2PR_*` setting or a stray `GIT_CONFIG_*` value cannot leak
into a coding run by default — someone has to add it deliberately.

**The prompt goes on stdin, never on argv.** It is untrusted work-item text, it can be tens
of kilobytes, and argv is world-readable on the host.

**Per-run temp directories, always cleaned up.** Including on the failure path. Clones never
accumulate in `/work`.

**Errors are mapped where they happen.** Every layer raises `RunFailure(code, message)`
rather than a bare exception, so no generic handler has to guess and nothing collapses a
specific code into `internal`.

## 6. Trust boundaries

| Boundary | What crosses it | What does not |
|---|---|---|
| Caller → agent endpoint | The contract body, over TLS, with an Entra token the Foundry gateway validates. | The caller's `Authorization` header — the gateway strips it. The agent authorizes on `T2PR_ALLOWED_ORGS`, not on the caller. |
| Agent → Azure DevOps | `T2PR_ADO_PAT`, a Personal Access Token scoped to the single organization in `T2PR_ALLOWED_ORGS` and to Work Items (Read), Code (Read & Write) and Pull Request (Read & Write) — nothing else ([deploy.md §3](deploy.md#3-azure-devops-access-create-a-pat)). It is read from the environment by the one token provider in `ado.py`. | The agent identity — Azure DevOps will not accept it. And the PAT itself never crosses any *other* boundary: not into a log, a commit message, a pull request body, argv, or the harness environment. |
| Agent → harness subprocess | The repository files, the prompt on stdin, one Entra token scoped to `https://ai.azure.com/.default`, and an allow-listed environment of search paths, locales and CA bundles. | The Azure DevOps PAT — **and every managed-identity variable that could be exchanged for one**: `IDENTITY_ENDPOINT`/`IDENTITY_HEADER`, `MSI_ENDPOINT`/`MSI_SECRET`, `IMDS_ENDPOINT`, `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`. Also `T2PR_*` settings and `.git/config` credential helpers. |
| Harness → model | The inference token above, minted once per run by the *parent* process. The harness holds a credential, not the ability to obtain one — the identity endpoint mints tokens for any audience, so reaching it is equivalent to holding every token the agent identity can get. | Anything long-lived, and any audience but inference. |
| Harness → agent's callers | Nothing directly. Its summary, stdout and stderr are redacted, capped, and (in the pull request body) fenced as a code block before they reach a log, a PR description, a commit message or an `error.message`. | Unredacted output; markdown that renders. |

The generated code is executed only inside the agent's own sandbox, during the run, to the
extent the harness runs tests. It is never merged. See [../SECURITY.md](../SECURITY.md).

## 7. Testing

The shipped suite lives in `agent/tests/`. Two rules make it runnable by anyone who clones
the repository:

- **No test may require model access or a live Azure DevOps organization.** Coding runs go
  through `FakeHarness`; Azure DevOps goes through the local stub in
  `agent/scripts/dev_ado_stub.py`. Git tests clone from and push to real bare repositories
  over `file://`, which is why CI sets a global git identity.
- **`FakeHarness` is a shipped component, not test scaffolding.** It implements the same
  protocol as the real harnesses, which is what lets the whole orchestration be exercised
  on a laptop with no Azure tenant — see
  [`agent/README.md §3`](../agent/README.md#3-run-it-locally-end-to-end).

What the suite has to cover:

| | |
|---|---|
| Unit | Request validation, prompt building and truncation, branch name validation, error mapping, the harness factory, redaction. |
| Contract | `run.py` end to end against a recorded Azure DevOps fixture set and a fake harness that writes one file — fetch, branch, code, commit, push, pull request. |
| Invariants | Every one of the eleven error codes actually observed; nothing pushed before the harness succeeds; no dangling remote branch; the harness environment allowlist, asserted by name. |

CI (GitHub Actions) runs lint (`ruff`), type check (`mypy --strict` on `src/`), the tests
and a `docker build`. **No deploy in CI.**
