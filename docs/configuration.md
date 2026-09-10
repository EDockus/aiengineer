# Configuration

Every setting is an environment variable. There is no config file, no database and no
control-plane API. `agent/src/ticket_to_pr/config.py` reads and validates all of them at
startup and **refuses to start** rather than run with a setting it does not understand —
a bad value is a crash with a message, never a silent default.

In a deployed agent these come from the `env:` map of the `ticket-to-pr` service in
[`azure.yaml`](../azure.yaml).

---

## 1. Settings

| Variable | Default | Notes |
|---|---|---|
| `T2PR_HARNESS` | `codex` | `codex` or `claude-code`. (`fake` exists but is dev-only — see §3.) |
| `T2PR_MODEL_DEPLOYMENT` | `gpt-6` | Name of a model deployment in the Foundry project. Must be compatible with the harness. |
| `T2PR_ALLOWED_ORGS` | *(none — required)* | Comma-separated Azure DevOps organization allowlist. An empty allowlist rejects every request, so startup fails if it is unset. Matching is case-insensitive. |
| `T2PR_MAX_RUN_MINUTES` | `20` | Hard timeout for the harness step. Range 1–240. Exceeding it fails the run with `timeout`. |
| `T2PR_MAX_FILES_CHANGED` | `50` | Safety rail. More files changed than this fails the run with `harness_failed`. Range 1–10000. |
| `T2PR_PR_DRAFT` | `true` | Create the pull request as a draft. Accepts `1/true/yes/on` and `0/false/no/off`. |
| `T2PR_PR_REVIEWERS` | *(empty)* | Comma-separated Azure DevOps user or group ids to add as reviewers. |
| `T2PR_COMMIT_AUTHOR_NAME` | `Ticket-to-PR agent` | Git author name on the commit. |
| `T2PR_COMMIT_AUTHOR_EMAIL` | `ticket-to-pr@noreply.local` | Git author email. Must contain `@` and no whitespace. |
| `T2PR_ADO_PAT` | *(none)* | **Secret.** Azure DevOps Personal Access Token. **Required** outside dev mode — the container refuses to start without it. Scopes: Work Items (Read), Code (Read & Write), Pull Request (Read & Write). See [deploy.md §3](deploy.md#3-azure-devops-access-create-a-pat) — and, for why a PAT rather than the agent identity, [the decision record](deploy.md#why-not-the-agent-identity--the-decision-record). |
| `T2PR_LOG_LEVEL` | `INFO` | One of `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`. Case-insensitive. |
| `T2PR_HOST` | `0.0.0.0` | Bind address. `127.0.0.1` in dev mode; an explicit non-loopback value is refused in dev mode. |
| `T2PR_PORT` | `8000` | Listen port. **Set this to `8088` when deploying to Foundry** — see below. |

`azure.yaml` exposes `T2PR_HARNESS`, `T2PR_MODEL_DEPLOYMENT` and `T2PR_ALLOWED_ORGS` as
`${...}` references into the `azd` environment; the rest are literals you edit in the
file. See [deploy.md](deploy.md#2-configure-the-three-parameters) for how to set them.

### `T2PR_PORT` and the hosted-agent runtime contract

`T2PR_PORT` is not a setting of the agent's own choosing. It is here because the Foundry
hosted-agent runtime contract requires the container to listen on **port 8088**, while the
image's own default is 8000. `azure.yaml` therefore sets `T2PR_PORT: "8088"`. If you deploy
the image by some other route, set it yourself or the platform's health probe never
connects.

> Verified against
> [Hosted agent runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract)
> (ms.date 2026-07-30): "Listen on port 8088", "Default port 8088 (override with the
> `PORT` environment variable)".

---

## 2. Variables the platform injects — do not set these

| Variable | Set by | Used for |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry (and `azd ai agent run` locally) | Model endpoint for both harnesses. |
| `FOUNDRY_AGENT_NAME`, `FOUNDRY_AGENT_VERSION`, `FOUNDRY_AGENT_SESSION_ID`, `FOUNDRY_PROJECT_ARM_ID` | Foundry | Identity and telemetry context. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Foundry | OpenTelemetry export. |
| `IDENTITY_ENDPOINT`, `IDENTITY_HEADER`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE` | The hosting platform | `DefaultAzureCredential` uses these to obtain the agent identity's tokens. |

**`FOUNDRY_PROJECT_ENDPOINT` must never appear in `azure.yaml`.** The platform reserves
the `FOUNDRY_` and `AGENT_` prefixes and declaring one shadows the platform value. The
agent reads it directly from the environment (`harness/__init__.py: foundry_endpoint()`),
which is also why setting it is what makes dev mode refuse to start (§3).

---

## 3. Dev-only variables

Two variables exist purely so the whole orchestration can be exercised on a laptop with
no Azure tenant, against the local Azure DevOps stub in `agent/scripts/dev_ado_stub.py`.
**Neither can be enabled in a deployed image.**

| Variable | Default | Notes |
|---|---|---|
| `T2PR_DEV_MODE` | *(unset)* | Must be exactly `i-understand-this-disables-auth`. Any other non-empty value is a startup **error**, not a silent "off". |
| `T2PR_ADO_BASE_URL` | `https://dev.azure.com` | Azure DevOps base URL. Any value other than the default is a startup error unless dev mode is on. |

`T2PR_HARNESS=fake` selects a harness that writes one deterministic file and returns a
canned summary instead of calling a model. It is also dev-mode-only.

### Why they cannot be turned on in the deployed image

`config.py: _validate_dev_mode` refuses to start unless **every** one of these holds:

1. `T2PR_DEV_MODE` is the exact confirmation phrase.
2. `/etc/ticket-to-pr/production` does not exist. `agent/Dockerfile` creates that file, so
   dev mode is unavailable in any built image without rebuilding it.
3. None of `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_PROJECT_ENDPOINT`, `CONTAINER_APP_NAME`,
   `CONTAINER_APP_ENV_DNS_SUFFIX`, `WEBSITE_INSTANCE_ID`, `WEBSITE_SITE_NAME`,
   `KUBERNETES_SERVICE_HOST`, `IDENTITY_ENDPOINT`, `MSI_ENDPOINT` or
   `AZURE_FEDERATED_TOKEN_FILE` is set — that is, this is not a cloud host.
4. The listener binds to loopback.
5. `T2PR_ADO_BASE_URL` is set and is an `http(s)` URL on `127.0.0.1`, `localhost` or `::1`.

Conditions 2 and 3 each independently make dev mode impossible in a Foundry-hosted
container. Conversely, with dev mode **off**, `T2PR_ADO_BASE_URL`, `T2PR_DEV_ADO_TOKEN`
and `T2PR_HARNESS=fake` are each a hard startup error rather than a silently ignored
setting, so a half-configured deployment fails loudly.

See [`agent/README.md`](../agent/README.md#3-run-it-locally-end-to-end) for the local
end-to-end walkthrough these enable.

---

## 4. Harness configuration — what was verified

The exact CLI flags and Foundry authentication variables for each coding CLI are checked
against current documentation rather than guessed — that is a standing requirement, not a
one-off ([CONTRIBUTING.md → Adding a harness](../CONTRIBUTING.md#adding-a-harness)). This
section records the check. Dates are the `ms.date` / publication date of the page consulted;
everything here was re-verified on **2026-09-09**.

Neither harness ever receives an Azure DevOps token, **or the means to obtain one**.
`harness/__init__.py: clean_env` builds the subprocess environment from an **allowlist**
(`ENV_ALLOWLIST`), not by filtering `os.environ`, and `git_ops.strip_credential_config`
clears `.git/config` of credential helpers immediately before the harness starts.

> **Why the harness is handed a token instead of a credential.**
> The obvious design is to let each CLI resolve the managed identity itself — Claude Code
> has a default-credential-chain mode, and Codex has a command-backed `auth` block. Both
> require the managed-identity variables (`IDENTITY_ENDPOINT` + `IDENTITY_HEADER`,
> `MSI_ENDPOINT` + `MSI_SECRET`, or `AZURE_FEDERATED_TOKEN_FILE` + `AZURE_CLIENT_ID` +
> `AZURE_TENANT_ID`) to be present in the harness environment. **The identity endpoint
> mints a token for whatever audience the caller names**, including Azure DevOps'
> `499b84ac-1321-427f-aa17-267ca6975798`. A work-item description — text anyone who can
> file a ticket controls, which reaches the harness verbatim in the prompt — could
> therefore have talked the harness into minting an Azure DevOps token with write access
> to every repository the agent identity can reach. None of those variables is in
> `ENV_ALLOWLIST`, and `tests/test_harness.py` fails the build if any of them returns.
>
> Instead the **parent process** holds the identity, mints one token for
> `https://ai.azure.com/.default` per run
> (`harness/__init__.py: foundry_token`) and passes its *value* to the CLI. A harness that
> leaks its whole environment leaks an inference token for the model deployment it was
> already given.
>
> **Token lifetime.** Neither CLI can refresh a token supplied this way. Entra access
> tokens for that scope are typically valid for an hour or more, comfortably longer than
> the 20-minute `T2PR_MAX_RUN_MINUTES` default; if you raise that setting towards its
> 240-minute ceiling the token can expire mid-run, the CLI starts getting `401`s and the
> run ends as `harness_failed`. The agent logs a `WARNING` naming both numbers when the
> token it just minted expires before the run budget does — keep the budget inside the
> token's lifetime.

### Codex (`T2PR_HARNESS=codex`)

Invocation:

```
codex exec --ignore-user-config --skip-git-repo-check \
      --sandbox workspace-write --cd <repo> --model <T2PR_MODEL_DEPLOYMENT> \
      --output-last-message <file> \
      -c approval_policy="never" \
      -c model_provider="foundry" \
      -c model_providers.foundry.name="Microsoft Foundry" \
      -c model_providers.foundry.base_url="<FOUNDRY_PROJECT_ENDPOINT>/openai/v1" \
      -c model_providers.foundry.wire_api="responses" \
      -c model_providers.foundry.env_key="FOUNDRY_AUTH_TOKEN"
```

with the prompt on stdin, and `FOUNDRY_AUTH_TOKEN` in the environment holding the bearer
token the parent minted for this run.

| Claim | How it was verified |
|---|---|
| `codex exec` is the non-interactive subcommand and reads the prompt from stdin when the prompt argument is omitted | Codex CLI docs, non-interactive mode |
| `--sandbox {read-only,workspace-write,danger-full-access}` | Codex CLI docs, approvals and sandboxing |
| **`--ask-for-approval` is *not* accepted by `codex exec`** — it exists on the top-level `codex` command only, and `codex exec` rejects it as an unexpected argument. The approval policy is set through the `approval_policy` config key instead. | Corrected against the documentation by running `codex exec --help` on the pinned `codex-cli 0.153.4` in the image |
| `-m/--model`, `-C/--cd`, `--skip-git-repo-check`, `--ignore-user-config`, `--output-last-message`, repeatable `-c/--config key=value` with TOML values | Codex CLI reference |
| `model_provider` and `model_providers.<id>.{name,base_url,wire_api,env_key}`. `env_key` "specifies which environment variable contains the provider API key"; Codex reads it at request time and sends the value as the `Authorization: Bearer` header. | Codex config-file reference |
| The command-backed `model_providers.<id>.auth.{command,timeout_ms,refresh_interval_ms}` block also exists and does refresh — but it is **deliberately not used**, because making it work meant putting the managed-identity plumbing in the harness environment. See the box above. | Codex config-file reference |
| `<project endpoint>/openai/v1` is a valid OpenAI-compatible base URL that needs only project-level permissions | [Migrate hosted agents](https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview) shows `base_url=f"{FOUNDRY_PROJECT_ENDPOINT}/openai/v1"` and states it "uses the project-scoped endpoint, which requires only project-level permissions" |

The value of `FOUNDRY_AUTH_TOKEN` is an Entra access token for
`https://ai.azure.com/.default`, minted by the agent process at the start
of the run. No secret is baked into the image, and the image installs no token helper the
harness could call for a second one.

> **Known limitation — read this before choosing Codex.**
> Microsoft's Codex-on-Foundry page
> ([learn.microsoft.com/azure/foundry/openai/how-to/codex](https://learn.microsoft.com/azure/foundry/openai/how-to/codex),
> ms.date **2026-09-03**) states in its troubleshooting table, verbatim:
> **"Entra ID support is currently not available for Codex."** The only authentication
> that page documents is an API key via `env_key = "AZURE_OPENAI_API_KEY"`, which this
> project will not use — model access here is keyless
> ([SECURITY.md → Credentials](../SECURITY.md#credentials)). The upstream request,
> [openai/codex issue #2024](https://github.com/openai/codex/issues/2024), is still open.
>
> Every configuration key used above is individually documented, but the *combination* of
> a Foundry endpoint and an Entra bearer token is not a documented, tested flow. **A 401
> from the Codex harness against Foundry is the expected outcome until Entra support
> ships.** If you need this working today, use `claude-code`.

### Claude Code (`T2PR_HARNESS=claude-code`)

> **Switching from `codex` to `claude-code` removes a sandbox.**
> `codex exec` runs with `--sandbox workspace-write`, which confines writes to the working
> tree and is enforced by the CLI itself. `claude --permission-mode bypassPermissions` has
> **no sandbox at all**: the harness may read and write anywhere the container's user can,
> and may run any command, without prompting. After the switch **the container is the only
> boundary** between generated code and everything the agent process can reach. That is an
> accepted trade — the container is non-root, per-run, and torn down after the run — but it
> is a real difference, and it is the reason the Dockerfile's non-root user and the
> `ENV_ALLOWLIST` above are load-bearing rather than decorative.

Invocation:

```
claude --print --output-format json --permission-mode bypassPermissions \
       --model <T2PR_MODEL_DEPLOYMENT>
```

with the prompt on stdin, and this environment:

| Variable | Value | Why |
|---|---|---|
| `CLAUDE_CODE_USE_FOUNDRY` | `1` | Enables the Foundry provider. |
| `ANTHROPIC_FOUNDRY_BASE_URL` | the **account** endpoint plus `/anthropic` — `https://{resource}.services.ai.azure.com/anthropic`, derived by stripping `/api/projects/{name}` off `FOUNDRY_PROJECT_ENDPOINT` | Foundry's Anthropic route lives on the account host, not under the project path. See the box below. |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | all `<T2PR_MODEL_DEPLOYMENT>` | Aliases do not resolve to your deployment names, so every alias is pinned. |
| `ANTHROPIC_FOUNDRY_API_KEY` | **deliberately unset** | Model access is keyless: an Entra token from the agent's own identity, never an API key ([SECURITY.md → Credentials](../SECURITY.md#credentials)). |
| `ANTHROPIC_FOUNDRY_AUTH_TOKEN` | the run's inference token | Documented option C: "Claude Code sends the value of `ANTHROPIC_FOUNDRY_AUTH_TOKEN` on every request as the `Authorization: Bearer` header. Use this option when another process … has already obtained an access token for you. Requires Claude Code v2.1.203 or later" — the image pins 2.1.266 — and it "takes precedence over `ANTHROPIC_FOUNDRY_API_KEY` and over the default credential chain". Chosen over option B (the default credential chain) because option B needs the identity plumbing in the harness environment; see the box above. |
| `DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DO_NOT_TRACK` | `1` | Neither CLI should phone home from inside a customer tenant, and an autoupdate would break the pinned image. |

| Claim | How it was verified |
|---|---|
| `-p/--print` is print (non-interactive) mode and accepts the prompt on stdin | Claude Code CLI reference and headless mode docs |
| `--permission-mode` accepts `bypassPermissions`; `--dangerously-skip-permissions` is documented as equivalent. The explicit spelling is used. | Claude Code permission-modes docs |
| bypassPermissions "refuses to start when running as root or under sudo" | Same page — which is *why* `agent/Dockerfile` runs as a non-root user. This is a correctness requirement, not just hardening. |
| `--output-format {text,json,stream-json}`; the `json` object carries the final assistant text in `result`, plus `subtype`, `is_error`, `num_turns`, `total_cost_usd` | Claude Code headless docs |
| `CLAUDE_CODE_USE_FOUNDRY=1` plus `ANTHROPIC_FOUNDRY_BASE_URL` (or `ANTHROPIC_FOUNDRY_RESOURCE`) enables Foundry | [code.claude.com/docs/en/microsoft-foundry](https://code.claude.com/docs/en/microsoft-foundry), step 3 |
| **Three documented authentication options.** Option B: "When neither `ANTHROPIC_FOUNDRY_API_KEY` nor `ANTHROPIC_FOUNDRY_AUTH_TOKEN` is set, Claude Code automatically uses the Azure SDK default credential chain" — failures surface as `Failed to get token from azureADTokenProvider`. Option C is the bearer token this agent uses. | Same page, "Configure Azure credentials", options B and C, and Troubleshooting |
| Model aliases (`sonnet`/`opus`/`haiku`) resolve to Claude Code's built-in Foundry defaults rather than to your deployments, and must be pinned | Same page, step 4 ("Pin model versions") |
| `Azure AI User` and `Cognitive Services User` include the permissions needed to invoke Claude models | Same page, "Azure RBAC configuration" |

> **Settled against a live deployment.** An earlier revision of this page called the base
> URL unverified and had the harness build `<FOUNDRY_PROJECT_ENDPOINT>/anthropic`, on the
> theory that the project path would keep the agent identity's access project-scoped. It
> does not work. Foundry does **not** route `/anthropic` under the project path: the
> request reaches Foundry's generic project API instead of the Anthropic route and comes
> back
>
> ```
> 400 Missing required query parameter: api-version
> ```
>
> — an error about a parameter the correct route never takes, which is why it is hard to
> read. The account endpoint that
> [code.claude.com/docs/en/microsoft-foundry](https://code.claude.com/docs/en/microsoft-foundry)
> documents is the right one, and `_anthropic_base_url` in `harness/claude_code.py` derives
> it by dropping the project segment.

> **That makes the account scope load-bearing, not a fallback.** Account-level
> capabilities are not proxied by the project endpoint, so the agent identity needs a model
> role at **Cognitive Services account scope** — which grants inference against *every*
> model deployment on that account, not just this project's. There is no project-scoped
> alternative for this route. The clean answer is to give the agent its own Foundry
> account, so account scope and project scope coincide; `scripts/setup.sh`'s create path
> does exactly that. See
> [deploy.md → the agent's own model access](deploy.md#the-agents-own-model-access--azd-grants-it-nothing).

### Which harness to pick

**Use `claude-code`.** Its Entra path is documented, supported and needs no API key.
Codex's Foundry integration currently documents only key-based auth, and this project does
not use keys. Revisit when
[openai/codex#2024](https://github.com/openai/codex/issues/2024) closes.

---

## 5. Related

- [contract.md](contract.md) — the request and response the agent serves.
- [deploy.md](deploy.md) — how to set these on a real deployment.
- [architecture.md](architecture.md) — where each of these is read.
