# Security

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub's
[private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
on this repository, go to **Security → Report a vulnerability**. That opens a draft
advisory visible only to you and the maintainers.

Please include:

- what an attacker can do, and what they need to start (a token? an org membership? a work
  item they can edit?);
- the smallest reproduction you have — a request body, a work-item field, a configuration;
- the version or commit you tested;
- whether you have disclosed it anywhere else.

**What to expect.** Acknowledgement within 3 working days. An assessment with a severity
and a rough timeline within 10 working days. We will keep you updated while we work on a
fix, credit you in the advisory and the changelog unless you ask us not to, and publish the
advisory when the fix ships. If we conclude the report is not a vulnerability we will
explain why rather than closing it silently.

Please give us 90 days before public disclosure, or less if a fix ships sooner.

### Out of scope

- Vulnerabilities in Azure DevOps, Microsoft Foundry, the Codex CLI or the Claude Code CLI
  themselves. Report those to their vendors; tell us too if this project's configuration
  makes them worse.
- Anything that requires an attacker to already hold the agent's Entra identity or
  `Contributor` on the Azure subscription.
- Findings from a scanner with no demonstrated impact.
- The dev-mode path (`T2PR_DEV_MODE`) running on a developer's own loopback interface. It
  disables authentication *by design* and cannot be enabled in a deployed image — but a way
  to enable it in one **is** in scope, and interesting.

## What this agent does with generated code

This matters more than anything else here, so it is stated plainly.

**The agent runs model-generated code only inside its own container, and it never merges
anything.**

Concretely:

- The coding harness runs inside the agent's per-session Foundry sandbox, on a shallow
  clone in a per-run temporary directory. That directory is deleted at the end of every
  run, including on the failure path.
- The agent's output is a **draft pull request** (`T2PR_PR_DRAFT` defaults to `true`). It
  does not approve, does not complete, does not merge, does not set auto-complete, and does
  not touch branch policies. A human reviews and merges, or does not.
- The agent pushes to a **new branch only**. The branch must not already exist, or the run
  fails with `branch_exists`. It never force-pushes and never writes to the base branch —
  its Azure DevOps grant does not include *Force push* or *Bypass policies when pushing*.
- If the pull request cannot be created after a push, the agent **deletes the remote branch**
  before reporting `pr_failed`, so a failed run leaves nothing behind — including when the
  process is being shut down mid-run. The one exception is deliberate: if creation failed
  *ambiguously* (a timeout, where Azure DevOps may have created the pull request anyway) the
  agent checks first and leaves the branch alone if a pull request exists on it. Deleting
  the source branch of a live pull request is the worse outcome.
- `T2PR_MAX_FILES_CHANGED` (default 50) aborts a run whose diff is implausibly large.
- `T2PR_MAX_RUN_MINUTES` (default 20) bounds the run. The agent signals the harness's
  **whole process group**, not just the CLI, on **every** exit path — success, failure and
  timeout alike — so nothing the harness started outlives the run. Two earlier versions got
  this wrong in turn: the first signalled only the direct child, so grandchildren kept
  running; the second signalled the group but only on timeout, so a harness that forked a
  detached child and then exited 0 left it alive, and it was still writing into a *later*
  run's working directory minutes afterwards.
- The per-run working directory is deleted when the run ends, including on the failure path.
  The container is **not** torn down with it: this is a hosted agent that holds its runs in
  an in-memory store across invocations, so one long-lived process serves many runs. That is
  why killing the process group on every exit path matters, and why the harness runs as a
  separate uid that cannot write the server's `$HOME` — a run's blast radius has to be
  bounded by the agent's own controls, not by a sandbox being thrown away afterwards.
- Every git command the agent runs after the harness returns is hardened against the working
  tree the harness just had write access to. `.git/hooks/` is emptied and `.git/config` is
  stripped of both credential keys and every key whose value git executes as a command
  (`core.hooksPath`, `core.fsmonitor`, `core.sshCommand`, `filter.*`, `diff.*.command`,
  `*.textconv`, `include*`), and `core.hooksPath` is *additionally* forced on the command
  line, where nothing on disk can redirect it. `git push` — the one command that carries the
  Azure DevOps PAT — also passes `--no-verify`. Without this a planted `.git/hooks/pre-push`
  ran as the agent's uid with `AUTHORIZATION: basic <PAT>` in its environment.

To be explicit about the residual risk: a coding harness executes commands in its working
directory, which is how it runs your test suite. Generated code therefore *does* execute —
inside the sandbox, during the run, with the agent's network access. It does not execute in
your CI, on your machine, or against your production systems as a result of anything this
agent does.

## Credentials

- **Two credentials, and they are not alike.**
  - **Foundry models: keyless.** A short-lived Entra token from the agent's own identity via
    `DefaultAzureCredential`. Nothing stored, nothing to rotate.
  - **Azure DevOps: a Personal Access Token** (`T2PR_ADO_PAT`). This is a long-lived secret
    and the sharpest edge in the design. It is a deliberate decision, not an oversight:
    Azure DevOps will not entitle a Foundry Entra agent identity (`VS403283`, while an
    ordinary service principal in the same organization is accepted), and that identity
    cannot federate into one Azure DevOps does accept (`AADSTS700231` — a token obtained
    through a federated credential may not be used as one). The full decision record, with
    every route tested, is in
    [docs/deploy.md](docs/deploy.md#why-not-the-agent-identity--the-decision-record).

  **What that costs you, stated plainly.** A PAT is valid until somebody revokes it, so a
  leak is not self-healing the way an expired token is. It carries its creator's access
  capped by its scopes, so it is only as narrow as you made it — grant Work Items (Read),
  Code (Read & Write) and Pull Request (Read & Write), scoped to one organization, and
  nothing else. Rotate it on a schedule. If it ever appears in a log, a screenshot or a
  shared terminal, revoke it first and investigate after.

  **What the agent does to contain it.** Sent as HTTP `Basic`, never `Bearer`. Passed to
  `git` through `GIT_CONFIG_COUNT` environment variables — never argv, which `ps` can read,
  and never written into `.git/config`. Excluded from the harness environment by the
  allowlist, which drops every `T2PR_*` variable, and the harness runs as a different uid.
  Kept off the **exec-time** environment of every process in the container by the launcher
  (`CREDENTIAL_NAME_FRAGMENTS` in `agent/src/ticket_to_pr/launch.py`), so
  `/proc/<pid>/environ` yields nothing for it either — see the exec-time environment section
  below for why that is a separate defence and not a restatement of the allowlist.
  Registered with the redactor at startup — the raw token in `main.create_app` and the
  base64 `Basic` form in `PatTokenProvider.__init__`, because redaction is by value and
  those are two different strings — so it is struck from harness output, git stderr, log
  lines, `error.message` payloads and pull request bodies **by value**, not only by shape:
  shape alone cannot recognise a PAT, which is why that layer exists
  (`agent/tests/test_redact.py`).

  **The gap this closed, recorded rather than quietly fixed.** For four review rounds the
  sentence above was half true. `launch.py` scrubbed variables whose names matched
  `IDENTITY`, `MSI`, `SECRET`, `TOKEN` and so on — and `T2PR_ADO_PAT` matches none of them.
  So it stayed in the environment the launcher `execve`d the **runner** with, and the
  runner has already dropped to the harness's own uid: `/proc/<runner>/environ` was a
  permitted same-uid read for every coding CLI the runner spawned. Anyone who could file a
  work item could have had the harness read it out and POST it anywhere, and egress is
  unrestricted because the harness must reach Foundry. The uid split did not help — the
  secret was on the harness's *own* side of it — and `redact()` never saw the traffic.
  The two fragment lists are now one list, pinned to each other by
  `agent/tests/test_launch.py::test_the_two_fragment_lists_are_the_same_list`, and
  `agent/tests/boundary_scenario.py` probes the runner's environ for a PAT sentinel, not
  only the server's for an identity one.
- **The image contains no secrets**, including no `.npmrc` registry token.
- **The harness never sees the Azure DevOps credential, or the means to mint one.** Its
  subprocess environment is built from an allowlist (`harness/__init__.py: ENV_ALLOWLIST`),
  not by filtering the parent environment, and `.git/config` credential helpers are
  stripped immediately before the harness starts.

  The second half of that sentence is the part that took two attempts. An earlier version
  allow-listed the managed-identity plumbing (`IDENTITY_ENDPOINT`/`IDENTITY_HEADER`,
  `MSI_ENDPOINT`/`MSI_SECRET`, `AZURE_FEDERATED_TOKEN_FILE` with `AZURE_CLIENT_ID` and
  `AZURE_TENANT_ID`) so each CLI could obtain its own model credential. **That was
  equivalent to handing over an Azure DevOps token**: the identity endpoint issues a token
  for whatever audience the caller names, so a work-item description could have instructed
  the harness to request the Azure DevOps resource id and exfiltrate the result. None of
  those variables is in the allowlist now, and `agent/tests/test_harness.py` fails the
  build if any of them comes back.

  Instead the **parent process** holds the identity, mints one token for
  `https://ai.azure.com/.default` per run, and passes its value to the CLI.
  A coding run gets repository files and an inference token for the model deployment it was
  already given; nothing else.

  That residual is not zero. `https://ai.azure.com/.default` is the **shared
  Azure AI Services data-plane audience**, so an inference token the harness manages to
  exfiltrate is valid against *every* Cognitive Services resource the agent identity holds
  RBAC on, not only this project's deployment. The size of that residual is set by the RBAC
  scope [`docs/deploy.md`](docs/deploy.md#the-agents-own-model-access--azd-grants-it-nothing)
  warns about: keep the identity scoped to the project, and the token is only good for the
  project's models.
- **Keeping the identity out of the harness's *reach*, not only its environment.** Dropping
  the managed-identity variables from the allowlist keeps them out of the harness's *own*
  environment. It does not keep them out of the harness's reach, and three separate review
  findings proved that by three different routes. Two of them existed only because the
  server and the harness once ran as the same uid: a harness could read the server's
  environment out of `/proc/<server>/environ` — which reflects the server's **exec-time**
  environment, unchangeable by anything the server does to `os.environ` afterwards — and it
  could write the server's `$HOME`, planting a `usercustomize.py` or a `.profile` that any
  later interpreter or login shell started by that uid would execute, holding the
  container's full unscrubbed configuration environment. On a Container Apps / App Service / IMDS deployment the platform
  injects `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` (or `MSI_ENDPOINT`/`MSI_SECRET`) into that
  exec-time environment, and a four-line stdlib payload run by the harness could read them
  back and mint an Azure DevOps token — the same outcome as the earlier finding, by a
  different route.

  The container ENTRYPOINT is therefore a launcher (`agent/src/ticket_to_pr/launch.py`),
  not the server. It runs as pid 1, reads every credential-shaped variable out of its
  environment, and `execve`s the server with them **removed from the environment it is
  exec'd with**, handing the values to the server on an inherited pipe fd the server drains
  and closes at the first line of startup. Because `/proc/<pid>/environ` is the exec-time
  environment, pid 1's environ comes back clean for a same-uid harness to read, while
  `DefaultAzureCredential` still finds the values in the server's in-process `os.environ`
  and `Settings.from_env` finds the PAT there. `agent/tests/test_launch.py` reproduces the
  `/proc` read and asserts neither scrubbed sentinel reaches a `run_cli` child.

  **It scrubs the runner too, and that hop is the one with no fallback.** The launcher
  `execve`s the unprivileged runner as well, from the same scrubbed environment. The runner
  wears the harness's own uid, so `/proc/<runner>/environ` is a *permitted* read for every
  CLI it spawns — no uid boundary stands between them, because they are the same uid by
  design. On that hop the scrub is not defence in depth; it is the only defence. Which is
  why `CREDENTIAL_NAME_FRAGMENTS` and `harness.FORBIDDEN_ENV_FRAGMENTS` are now literally
  the same tuple with a test pinning them together: a name defended on one boundary and not
  the other is a leak, and that is exactly how `T2PR_ADO_PAT` escaped for four rounds.
  Matching is a substring test on the upper-cased name, so the fragment is `ADO_PAT` and
  never `PAT` — `PATH` contains `PAT`, and a runner exec'd without `PATH` cannot spawn
  anything at all.

  The scrub alone was not enough, and the reason is worth stating: a launcher can clean the
  environment of the process it execs, but nothing can clean the container's *configuration*
  environment, which the runtime hands to every new process it starts. So the boundary is
  not the environment. It is the uid.

- **The harness runs as its own uid.** `agent/src/ticket_to_pr/runner.py` is an
  unprivileged runner process; every coding CLI is spawned by it, never by the server.

  | | uid | what it does |
  |---|---|---|
  | `agent` | 10001 | the server. Never executes model-generated code. |
  | `harness` | 10002 | the runner and every CLI it spawns. |
  | `t2pr` | gid 10003 | shared group, so one run directory can be written by the harness and read back, committed and deleted by the server. Nothing else is in it. |

  The launcher starts as root purely to hand the privilege away — it forks the runner and
  drops it to `harness`, then drops itself to `agent` before `execve`ing the server. By the
  time either process executes anything, nothing in the container is root, so the non-root
  requirement in [docs/architecture.md §4.4](docs/architecture.md#44-the-container-image)
  still holds (and so does the Claude Code CLI's refusal to run
  `bypassPermissions` as root). Dropping *is* a privilege: uid 10001 cannot become 10002 —
  verified, `EPERM` — which is why the launcher has to start with it.

  The runner is `execve`d, not merely forked, and that detail is load-bearing: a forked
  child keeps its parent's exec-time environ, so a fork alone would have left the whole
  identity plumbing readable at `/proc/<runner>/environ` — by a harness sharing that very
  uid. `/work` stays `0750 agent:agent`, so the harness cannot plant anything there; a
  harness's `$HOME` is its own per-run directory, deleted with the run.

  Verified in the built image, not merely intended — `agent/tests/test_runner.py::test_the_harness_uid_is_a_real_boundary`
  and `agent/tests/boundary_scenario.py` run a real harness through the real runner and
  assert it gets `denied` on `/proc/<server>/environ`, on `/proc/<server>/mem`, and on
  writing `/work` — and that the runner's environ, which it *can* read, holds neither
  sentinel:

  ```
  {"uid": 10002, "server_environ": "denied", "sentinel": false,
   "runner_environ": "readable", "runner_pat_sentinel": false, "runner_mi_sentinel": false,
   "server_home": "denied", "server_mem": "denied", "server_uid": 10001}
  ```

  `runner_environ: readable` is deliberately asserted rather than hoped away. It is the
  honest description of a same-uid read, and asserting it keeps the two `false`s meaningful:
  if `/proc` ever stopped answering, they would pass for the wrong reason. Before the fix
  the same probe on the same image returned `runner_pat_sentinel: true`.

  **This also closes the workload-identity residual.** An AKS federated-credential
  deployment projects `AZURE_FEDERATED_TOKEN_FILE` at a well-known path on disk, where
  scrubbing the variable achieves nothing. A separate uid does: provided the projected file
  is not world-readable, uid 10002 cannot open it. Check that on your own cluster if you
  deploy that way — file ownership is set by the platform, not by this image.
- **Tokens are never logged, and never copied into a pull request.** Not at any level — not
  the token, not the `Authorization` header. Full work-item bodies are not logged at `INFO`
  either, because they are customer content. Everything the harness prints — its summary,
  its stdout, its stderr — is passed through `redact()` before it can reach a log line, a
  pull request description or an `error.message` on the wire, because the harness may print
  a credential it obtained from a source the agent does not control.

## The container performs no inbound authorization

**This is the most important operational caveat in this document.**

`agent/src/ticket_to_pr/main.py` never reads the `Authorization` header and never checks
the caller's identity. That is by design: the agent is a **Microsoft Foundry hosted
agent**, and the Foundry gateway authenticates the caller's Entra token for the Agent
Application and strips the header before the request reaches this process
([docs/contract.md](docs/contract.md)). The gateway *is* the authentication.

The consequence: **anything that can reach the container's port can start a run.** Deploy
this image anywhere other than behind the Foundry agent endpoint — a Container App with
external ingress, a VM with a public IP, a Kubernetes Service of type LoadBalancer — and
you have an unauthenticated service that will create branches and pull requests in the
repositories the agent identity can write to.

If you must run it outside Foundry, put your own authenticating proxy in front of it and
do not expose the port. `T2PR_ALLOWED_ORGS` limits *which* organizations a caller can
reach; it does not limit *who* the caller is.

(The dev-mode path is the one case where the agent will admit this itself: it refuses to
start unless it is bound to loopback, talking to a loopback Azure DevOps stub, and running
on a filesystem without the production marker `agent/Dockerfile` bakes in.)

## Blast radius

The agent can only act on organizations listed in `T2PR_ALLOWED_ORGS`. An empty allowlist
rejects everything, and the agent refuses to start without one — there is no configuration
in which it is open by default.

Within an allowed organization it can do exactly what its Azure DevOps grant permits.
Grant it the four documented permissions per repository — *Read*, *Contribute*,
*Create branch*, *Contribute to pull requests* — and no more. Do not grant it *Force push*,
*Bypass policies*, *Manage permissions* or *Delete repository*. See
[docs/deploy.md §3](docs/deploy.md#3-azure-devops-access-create-a-pat).

The deploy guide is written to grant exactly that and nothing else: §3.1 puts the identity
in the project's **Readers** group, not *Contributors*, because Contributors carries write
on every repository in the project plus work-item and wiki write — which would make the
paragraph above false. If you followed an older revision of that guide, or another guide,
check the identity's group membership before believing this section.

Anyone who can invoke the agent can make it act on any repository in an allowed
organization. Treat the ability to call the agent endpoint as equivalent to the agent's own
Azure DevOps permissions, and scope the `Foundry Agent Consumer` role assignment
accordingly.

## Deployment time: what the setup scripts do on your machine

Everything above describes the **running agent**. `scripts/setup.sh` and
`scripts/teardown.sh` are a different threat model and deserve their own reading: they run
on your workstation, as you, with your `az login` still warm in `~/.azure`, at the one
moment in the project's life when a live Azure DevOps PAT exists in plaintext. Do not carry
the guarantees above — "never in argv", "runs as its own uid" — across into this section.
They are about the container.

**Run these scripts on a machine you trust, and not on a shared host.**

### The PAT is on a command line, exactly once

`setup.sh` routes every token it handles through `curl -K -`, which takes the
`Authorization` header on stdin, so no bearer token reaches argv. Step 5 is the single
exception, and it is not fixable in this repository: `azd ai connection create` takes the
secret as `--custom-key "pat=<token>"` and offers no stdin form. For the life of that one
process the PAT is readable in `/proc/*/cmdline` — by any other process running as your
uid, and by root. The script says so before it happens and asks for confirmation; the value
itself lands in the Foundry project's Key Vault, never in `.azure/` and never in a tracked
file. If the machine you ran it on is one you would not hand the token to directly, revoke
the token.

The same argv exposure applies to the `az account get-access-token` one-liners in the
documentation. Where those pipe into `curl -K -`, the token stays out of argv; where an
example writes `-H "Authorization: Bearer $TOKEN"`, it does not.

### `npm ci` fetches and runs third-party code as you

Step 9 builds the extension `.vsix`, which means `npm ci` in `extension/`: 318 packages
from `registry.npmjs.org`, every one pinned by `package-lock.json` to a version and an
integrity hash. That is a network reach and a supply chain, in a run where you have just
minted a live PAT and hold Azure refresh tokens for an identity that can register
subscription resource providers and mint organization PATs.

So the script runs `npm ci --ignore-scripts`. Without it, any of those packages'
`preinstall`/`install`/`postinstall` scripts would execute third-party code with all of
that reach. Only `esbuild` declares an install script, and its platform binary arrives as
an ordinary optional dependency the lockfile already pins, so the build does not need it.
If you build the extension by hand, use the same flag. The lockfile's integrity hashes are
what make this a bounded risk rather than an open one — do not update it without reading
the diff.

### `az provider register` is subscription-wide and is never undone

Preflight offers to register the resource providers `azd provision` needs. Registration is
a **subscription-scoped** change, not a resource-group one: it affects every deployment in
that subscription, it requires Contributor or Owner, and `teardown.sh` does not reverse it.
That is deliberate — unregistering a provider another workload depends on is a far worse
outcome than leaving a registration in place — but it does mean setup leaves one mark on
the subscription that teardown will not remove. The script asks first, and prints the exact
`az provider register` commands if you would rather hand them to whoever owns the
subscription.

### Teardown is destructive, and `--yes` removes the brakes

`teardown.sh` can reach `azd down --force --purge`, which deletes the resource group and
purges the soft-delete window on the Foundry account — there is nothing to restore
afterwards. It only offers to delete resources `setup.sh` recorded creating, in
`T2PR_SETUP_CREATED_*` / `T2PR_SETUP_GRANTED_*` markers in the azd environment: a Foundry
project, registry, `ado-secrets` connection, Entra app registration or AcrPull grant that
predates setup is left alone, because a Foundry project can be shared and deleting another
deployment's connection leaves that agent failing every run with `ado_forbidden`.

`--yes` answers every one of those prompts affirmatively, including that one. It exists for
scripted cleanup of a throwaway environment. Reaching for it against anything else means
the marker list is the only thing standing between you and the resource group.

## Reviewing what the agent produced

Read the pull request. It is generated code, and the model that wrote it saw the work item
text — which anyone who can edit a work item controls. Prompt injection through a work-item
description is a real avenue.

What it cannot do: obtain the Azure DevOps credential. The PAT is never placed in the
harness environment — the allowlist drops every `T2PR_*` variable — never written to
`.git/config`, and never passed in argv; the harness also runs as a different uid from the
server, so it cannot read the server's environment or memory through `/proc`. The harness's
own output is redacted, by value as well as by shape, before it reaches the pull request. It can still exfiltrate the *repository
contents* it was given, and anything else reachable from the container's network — a
coding harness that runs your test suite can also make an HTTP request.

What it can absolutely do: influence what the diff contains, and what the pull request
description says about it. The description's summary is written by the harness, so it is
**fenced as a code block rather than rendered as markdown** — a summary cannot forge a
heading or plant a link that reads like an approval. Read the diff, not the summary.

Review it the way you would review a pull request from someone you have never met.
