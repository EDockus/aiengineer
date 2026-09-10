# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The request/response contract in [`docs/contract.md`](docs/contract.md) is a published
interface: a breaking change to it bumps the major version **and** the path.

## [Unreleased]

### Added

- **`setup.sh` step 9 now builds the Azure DevOps extension package** instead of printing
  the four commands to build it. `npm ci`, `npm run configure` and `npm run package` need
  no credential the script does not already hold and reach nothing outside the repository,
  so the run ends with a `.vsix` path rather than homework. It stops at the upload, which
  needs a Marketplace personal access token — a second credential, and this script takes
  exactly one secret. The build is skipped, with the reason in one line and the commands
  printed as before, when the Marketplace publisher id or the repository URL is still blank
  (`configure.mjs` refuses to package a manifest carrying a `REPLACE-WITH-` placeholder) or
  the machine has no `node`/`npm`. It is never fatal: the agent is deployed and answering
  by the time step 9 runs.

### Changed

- **`docs/` is now the specification, and it is complete.**
  [`docs/contract.md`](docs/contract.md) is authoritative for anything a caller can see;
  [`docs/architecture.md`](docs/architecture.md) is authoritative for everything behind it
  and gained the parts that had only ever lived in working notes: the runtime and protocol
  (§2 — Invocations 1.0.0, background mode, the four routes, port 8088, the session idle
  timeout), the orchestration sequence step by step with the error code each step fails with
  and the no-dangling-branch rule (§3), the `Harness` protocol and factory and the rules
  every harness keeps (§4.2), prompt construction with the 8k field cap and HTML stripping
  (§4.3), the container requirements (§4.4) and what the test suite has to cover (§7). The
  Azure DevOps decision record — the four routes tested, `VS403283`, `AADSTS700231`, and the
  instruction not to reverse it without re-testing them — is now stated in full at
  [`docs/deploy.md §3`](docs/deploy.md#why-not-the-agent-identity--the-decision-record), and
  everything that referred to it points there. Code comments, test docstrings, CI steps and
  cross-references now cite a doc page and section rather than working notes.
- **`node`/`npm` are a soft preflight check in `setup.sh` step 1.** They build the
  extension in step 9 and nothing else in the script touches them, so a host without Node
  now gets a warning naming what will be skipped, and a working agent, instead of a `die`
  before step 1 finishes.
- `teardown.sh` step 9 also offers to delete `extension/dist/agent-config.json` and the
  `.vsix` in `extension/dist-vsix/`. Both are gitignored build outputs that carry the
  deployment's agent URL and Entra app registration, and step 9 of setup now creates the
  second one.

- **Azure DevOps authentication now uses a Personal Access Token** (`T2PR_ADO_PAT`),
  reversing the original "no API keys anywhere" goal for that path only. Foundry model
  access is unchanged and still keyless via the Entra agent identity. This is a decision
  forced by the platform, not an implementation shortcut — four routes were tested against
  a live tenant and organization and all failed:
  the agent identity cannot be entitled in an ADO organization (`VS403283`, while an
  ordinary service principal succeeded minutes later with the same call); it cannot federate
  into an app registration that ADO does accept (`AADSTS700231` — a federated token may not
  be used as a federated assertion); a hosted agent has no attachment point for a
  user-assigned managed identity; and while a UAMI *is* accepted by ADO, a hosted agent
  cannot run as one. Full decision record in
  [`docs/deploy.md §3`](docs/deploy.md#why-not-the-agent-identity--the-decision-record).
  `AuthMode.ENTRA` is replaced by `AuthMode.PAT`; `T2PR_ADO_PAT` is required outside dev
  mode and the container refuses to start without it.

### Security

- **The Azure DevOps PAT no longer sits in any process's exec-time environment.**
  `launch.py` scrubbed the managed-identity plumbing out of the environment it `execve`s
  the server and the unprivileged runner with, but `CREDENTIAL_NAME_FRAGMENTS` matched
  nothing in `T2PR_ADO_PAT` — so the PAT survived into the runner's exec-time environment,
  and the runner has already dropped to the *harness's own uid*. `/proc/<runner>/environ`
  is a permitted same-uid read, so every coding CLI the runner spawned could recover a
  long-lived org-wide `vso.work vso.code_write` token from its own parent, exfiltrate it
  over unrestricted egress, and never pass through `redact()`. Anyone able to file a work
  item could reach it, because work-item text reaches the harness verbatim in the prompt.
  The uid split did not help: the secret was on the harness's side of it.

  The two fragment lists (`launch.CREDENTIAL_NAME_FRAGMENTS` and
  `harness.FORBIDDEN_ENV_FRAGMENTS`) are now one list, pinned to each other by a test, and
  both gained `ADO_PAT` and `DEVOPS`. The fragment is `ADO_PAT` and not `PAT` because
  matching is a substring test on the upper-cased name and `PATH` contains `PAT` — a runner
  exec'd without `PATH` cannot spawn anything at all. `boundary_scenario.py` now probes the
  *runner's* environ for a PAT sentinel, not only the server's for an identity one; the
  previous probe passed for a reason that did not generalise. `PatTokenProvider` also
  registers the base64 `Basic` form with the redactor, not only the raw token.

- **Redaction now covers the PAT, by value and not only by shape.** A PAT is an
  undistinguished alphanumeric string, so the existing shape rules could never have found
  one in free text; `register_secret()` adds it as a literal at startup. The
  `Authorization` header pattern was also widened from `bearer` to any scheme, because
  Azure DevOps takes a PAT as HTTP `Basic` and `git` echoes the header it was given into
  stderr when a push is refused — a direct path into a pull request body that the previous
  `bearer`-only rule missed.

- **Run the coding harness under its own uid.** Three review findings in a row reached the
  managed identity by three different routes — the environment allowlist, then
  `/proc/<server>/environ`, then the server's writable `$HOME` — all because the server and
  the harness shared a uid. Each earlier fix closed one route without establishing that no
  fourth existed. The harness now runs as `harness` (10002) via an unprivileged runner
  process (`agent/src/ticket_to_pr/runner.py`), the server stays `agent` (10001), and a
  two-member group `t2pr` (10003) shares just the per-run directory. This retires the
  `/proc/<server>/environ` route, the `/proc/<server>/mem` route, the `$HOME` route, and the
  workload-identity residual at once. The launcher starts as root only to give the privilege
  away: uid 10001 cannot become 10002 (`EPERM`), so nothing else can drop the harness.
  Verified in the built image by `test_the_harness_uid_is_a_real_boundary`.
- **Stop the harness inheriting the server's `$HOME`, and isolate the healthcheck.** A
  harness could plant `$HOME/.local/lib/python*/site-packages/usercustomize.py`; the image's
  own `HEALTHCHECK` ran `python -c`, imported it every 30 seconds, and did so holding the
  container's unscrubbed configuration environment — silently, `ExitCode 0`. `HOME` is now a
  per-run directory (which also ends cross-run persistence via `/work/.claude` and
  `/work/.codex`), `PYTHONNOUSERSITE=1` is set for the harness, and the healthcheck runs
  `python -I`.
- **Harden the harness against recovering the managed identity from `/proc`.** Dropping the
  identity variables from the harness allowlist kept them out of the harness's environment
  but not out of its reach: the server and the harness run as the same uid, so a harness
  could read the server's exec-time environment from `/proc/<server>/environ` and mint an
  Azure DevOps token on a Container Apps / App Service / IMDS deployment. The container
  ENTRYPOINT is now a launcher (`agent/src/ticket_to_pr/launch.py`) that scrubs the identity
  plumbing out of the environment it exec's the server with, delivering the values to the
  server on an inherited pipe fd it drains at startup, so `/proc/<pid>/environ` stays clean
  while `DefaultAzureCredential` keeps working. The workload-identity (projected token file)
  residual is documented in `SECURITY.md`.
- **Bound the pull-request description against a backtick-run injection.** A work-item
  summary ending in thousands of backticks drove the code-fence marker arbitrarily wide and
  pushed the rendered description past Azure DevOps' 4,000-character limit, failing the run
  as `pr_failed` after the branch was pushed. Backtick runs in the summary are now clamped
  to three before fencing, pinning the marker at four.

- **A platform invocation id is validated before it is adopted as a run id.** The Foundry
  gateway's `x-agent-invocation-id` header is adopted as the run id so polling works through
  the gateway, and it was taken whole: a 5,000-character value, a value carrying CRLF, and a
  path-traversal string were all accepted, and the raw value reached the `Location` response
  header and INFO log lines. A repeat of an id already in use silently overwrote the first
  run, and since `GET /invocations/{id}` has no ownership check — it cannot have one, the
  platform authorizes callers before the request arrives — the first caller's next poll then
  returned the second caller's status, correlation id and error message. Ids must now match
  `^[A-Za-z0-9._-]{1,128}$`; anything else falls back to a generated UUID with a warning that
  logs the length, never the value. `RunStore.create` refuses to take a key already in use
  and mints a fresh id instead, so both runs survive.

- **`setup.sh` and `teardown.sh` no longer put a bearer token in argv.** Both `curl` calls
  to the Azure DevOps token lifecycle API now pass the `Authorization` header on stdin
  (`curl -K -`) rather than as `-H`, which `/proc/*/cmdline` exposes. `azd ai connection
  create --custom-key` has no stdin form, so that one exposure is disclosed instead of
  claimed away — in the file header and in a confirmation prompt before step 6 runs.

- **`teardown.sh` no longer deletes an Entra app registration it did not create.** It fell
  back to `az ad app list --display-name ticket-to-pr-hub` when the client id was
  unresolvable and deleted whatever came back; display names are not unique in a tenant and
  `az ad app delete` is not reversible. Deletion now requires
  `T2PR_SETUP_CREATED_ENTRA_APP=true` **and** a recorded `T2PR_ENTRA_CLIENT_ID`, with no name
  lookup anywhere. On the create side, `setup.sh` used to adopt a same-named app silently —
  while printing `skip ... (already done)` — and then rewrite its sign-in audience, replace
  its SPA redirect URIs, blank its web and publicClient ones, and add an `AllPrincipals`
  consent grant. It now shows the app id, display name and creation date and requires an
  explicit confirmation first.

- **`teardown.sh` no longer removes AcrPull grants it did not create.** `role_assign` skips
  silently when a grant already exists, which is common on a shared registry that something
  else deploys from, and teardown then offered to remove both — on a registry it correctly
  refused to delete. `setup.sh` records `T2PR_SETUP_GRANTED_ACRPULL_ACCOUNT` and
  `T2PR_SETUP_GRANTED_ACRPULL_PROJECT` on the create branch only, and removal is gated on
  them.

- **`codex exec --output-last-message` works again under the uid split.** `CODEX_HOME` was a
  bare `mkdtemp`, created 0700 by the server at uid 10001, while the CLI runs at 10002. The
  CLI could not create the file, reported no error, and the run summary silently degraded to
  a stdout tail. The scratch directory now lives inside the run directory and is shared with
  the harness group the same way, so the CLI writes it and the server reads it back.

### Documentation

- `SECURITY.md` and the `ado.py` module docstring both asserted containment for the PAT that
  did not hold; both are corrected, and the gap is recorded rather than quietly closed.
  `ado.py`'s 401/403 message blamed the agent identity, which is not what authenticates
  Azure DevOps requests any more — it now names an expired or revoked PAT first, which is
  the actual common cause.
- `README.md` now states, as two tables, exactly what `scripts/setup.sh` automates and what
  is left to a person, with the reason for each of the four manual steps — creating a
  Marketplace publisher, uploading and sharing the `.vsix`, installing it into the
  organization, and creating the Azure DevOps PAT by hand if you decline step 5's offer.
  "Automated" is qualified there rather than upgraded: no clean subscription has yet
  created a resource with this script.
- `setup.sh` claimed "nothing tenant-specific is written into a tracked file" while step 3
  rewrites the tracked `azure.yaml` with the deployer's Foundry account and project. azd
  reads the endpoint only from that file and will not take it from a variable, so this could
  not be moved; it is now stated plainly in the file header, at step 3, and in a step 9
  warning that fires when the file is actually modified, with the command to revert it.

## [0.1.0] - 2026-09-09

First release. A Microsoft Foundry hosted agent that turns an Azure DevOps work item into
a branch and a draft pull request.

### Added

- **Agent** (`agent/`) — Python 3.14 FastAPI service implementing the Foundry Invocations
  protocol. `POST /invocations` returns `202` with a run id, `GET /invocations/{runId}`
  reports phase and result, `GET /healthz` reports readiness. Orchestration is
  fetch work item → resolve repo → shallow clone → branch → run harness → commit → push →
  open pull request, with the phase visible at every step.
- **Contract v1** (`docs/contract.md`) — the published request and response, the eleven v1
  error codes, and a curl walkthrough. No `harness`, `model` or `coder` field: unknown
  fields are rejected with `400`.
- **Two coding harnesses** — `codex` (Codex CLI) and `claude-code` (Claude Code CLI), both
  pointed at a model deployment in the same Foundry project and authenticated with the
  agent's Entra identity. Selected at deploy time with `T2PR_HARNESS`.
- **A `fake` harness and a local Azure DevOps stub** (`agent/scripts/dev_ado_stub.py`) that
  run the entire orchestration against a real bare git repository with no Azure tenant and
  no model. Both are dev-mode-only and cannot be enabled in a built image.
- **Container** (`agent/Dockerfile`) — `python:3.14-slim`, Node LTS, both CLIs pinned,
  non-root, writable `/work`, no credentials and no `.npmrc`.
- **Deployment** (`azure.yaml`) — one `azure.ai.project` service owning a GPT-6 deployment
  and one `azure.ai.agent` service built from `agent/`: invocations protocol 1.0.0, cpu
  `1`, memory `2Gi`, a 30-minute session idle timeout, and the environment from
  `docs/configuration.md`. `FOUNDRY_PROJECT_ENDPOINT` is deliberately not declared — the
  platform injects it. No `infra/` directory; `azd` synthesises the infrastructure.
- **Azure DevOps extension** (`extension/`) — a hub page that lists work items and
  repositories, acquires an Entra token with MSAL, starts a run and polls it to the pull
  request link. Static, no backend.
- **Sample webhook receiver** (`examples/function-app/`) — Azure Functions Python v2. An
  HTTP trigger receives a work item service hook, filters on the `agent` tag, starts a run
  and comments the run id; a timer trigger polls and comments the pull request link.
  Managed identity throughout.
- **Documentation** — `docs/contract.md`, `docs/deploy.md`, `docs/configuration.md`,
  `docs/architecture.md`, `docs/extension.md`, plus `README.md`, `SECURITY.md` and
  `CONTRIBUTING.md`.
- **CI** (GitHub Actions) — ruff, `mypy --strict` on `src/`, pytest, an extension
  typecheck and build, and a Docker build that asserts the image is non-root and contains
  no credential files. No deploy step and no Azure credential in the workflow.

### Security

- No API keys and no personal access tokens anywhere. Every credential is a short-lived
  Entra token from `DefaultAzureCredential`; Azure DevOps tokens use the fixed scope
  `499b84ac-1321-427f-aa17-267ca6975798/.default`.
- The coding harness never receives an Azure DevOps credential **or the means to mint
  one**. Its environment is built from an allowlist, not by filtering the parent
  environment; the managed-identity variables are deliberately excluded, because the
  identity endpoint issues tokens for any audience including Azure DevOps. The parent
  process mints one inference-scoped token per run and passes its value to the CLI.
  `.git/config` credential helpers are stripped before it runs.
- Harness output — summary, stdout and stderr — is redacted, capped and (in the pull
  request body) fenced before it can reach a log line, a pull request description, a commit
  message or an `error.message`.
- The agent performs **no inbound authorization**; the Foundry gateway does. Do not deploy
  the image anywhere reachable outside that gateway. See `SECURITY.md`.
- Nothing is pushed until the harness succeeds, and if pull request creation fails after a
  push the remote branch is deleted before the failure is reported. A failed run leaves no
  dangling branch.
- The agent never merges. Pull requests are created as drafts by default.
- `T2PR_ALLOWED_ORGS` is required; an empty allowlist rejects every request and the agent
  refuses to start without one.
- Dev mode (`T2PR_DEV_MODE`) fails closed behind five independent checks, two of which are
  impossible to satisfy inside a deployed image.

### Known limitations

- **The Codex harness is expected to fail with `401` against Microsoft Foundry.**
  Microsoft's Codex-on-Foundry documentation (ms.date 2026-09-03) states "Entra ID support
  is currently not available for Codex" and documents only API-key authentication, which
  this project does not use. Upstream:
  [openai/codex#2024](https://github.com/openai/codex/issues/2024). Use `claude-code`.
- **Container protocol 1.0.0 is deprecated upstream.** `azure.yaml` declares `1.0.0`
  because on that version the platform propagates caller identity for the container. Moving
  to 2.0.0 requires the agent to forward the per-request `x-agent-foundry-call-id` header
  on outbound Foundry calls first.
- **Run state is in memory.** A run id does not survive a container replacement; callers
  should treat a `404` on a run id as "gone", not "failed".
- **A Claude model deployment cannot be declared in `azure.yaml`** — it needs a
  `modelProviderData` block the `deployments` schema has no field for. Create it in the
  Foundry portal or with the Claude on Foundry starter kit and point
  `T2PR_MODEL_DEPLOYMENT` at it. See [`docs/deploy.md §2`](docs/deploy.md#2-configure-the-three-parameters).

[Unreleased]: https://github.com/EDockus/aiengineer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/EDockus/aiengineer/releases/tag/v0.1.0
