# Ticket to PR — Azure DevOps extension

A hub under **Repos** that turns an Azure DevOps work item into a branch and a pull request by
calling the [ticket-to-pr agent](../README.md).

Pick a work item, pick a repository, accept or edit the suggested branch name, press **Create PR
with agent**. The page submits one request to the agent, polls it every 5 seconds, shows the phase
the run is in, and links to the pull request when it is done.

The extension is a static page. There is no backend of its own and no server component in the
package — it talks to Azure DevOps with the extension SDK's token and to the agent with an Entra
token for the signed-in user.

---

## Contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Entra app registration](#entra-app-registration)
- [Build](#build)
- [Package and install privately](#package-and-install-privately)
- [Configure the extension](#configure-the-extension)
- [Local dev harness](#local-dev-harness)
  - [Headless runtime checks](#headless-runtime-checks)
- [Project layout](#project-layout)
- [Security notes](#security-notes)
- [Known limitations](#known-limitations)
- [What was verified](#what-was-verified)

---

## How it works

```
 ADO hub page (this extension)
   │
   ├─ SDK.getAccessToken() ──► Azure DevOps REST 7.1
   │                             • POST wit/wiql            (work items assigned to you)
   │                             • GET  work/accountmyworkrecentactivity
   │                             • GET  wit/workitems       (batch, for titles/types)
   │                             • GET  git/repositories
   │
   └─ MSAL public client ────► Entra token for the Agent Application
        (PKCE, no secret)         │
                                  └─► POST {agentUrl}/invocations       → 202 { runId }
                                      GET  {agentUrl}/invocations/{id}  → every 5 s until done
```

The request body is exactly the published contract ([`docs/contract.md` §3](../docs/contract.md#3-request)):

```json
{
  "version": "1",
  "organization": "contoso",
  "project": "Platform",
  "workItem": { "type": "Bug", "id": 4123 },
  "repository": "payments-api",
  "branch": "bugfix/4123-null-ref",
  "baseBranch": "main",
  "correlationId": "..."
}
```

`baseBranch` is omitted when the field is left empty, so the agent uses the repository default.
There is deliberately **no `harness`, `model` or `coder` field** — the agent rejects unknown fields
with 400, and harness and model are deploy-time configuration.

All eleven contract error codes (`work_item_not_found`, `repository_not_found`, `branch_exists`,
`ado_forbidden`, `clone_failed`, `harness_failed`, `harness_no_changes`, `push_failed`, `pr_failed`,
`timeout`, `internal`) are rendered as sentences in `src/errors.ts`. The raw code is shown only as
secondary detail so it can be quoted in a support request.

---

## Prerequisites

- Node.js 20 or newer (developed against Node 23, npm 11).
- An Azure DevOps organization where you can install a private extension.
- A [Visual Studio Marketplace publisher](https://marketplace.visualstudio.com/manage) — free, and
  a private extension is only visible to organizations you share it with.
- The ticket-to-pr agent already deployed, and its Agent Application URL and audience to hand.

---

## Entra app registration

The page signs the **user** in and asks Entra for a token whose audience is the Agent Application.
That needs one app registration, which is a **public client — there is no client secret anywhere in
this package and there must never be one.**

Four things about it are non-obvious, and every one of them fails with an error that names
something other than its real cause. `docs/extension.md` [step 2](../docs/extension.md#2-register-the-entra-application)
has the full account, with the exact `az` commands and the exact error text; the short version:

1. In the [Azure portal](https://portal.azure.com) go to **Microsoft Entra ID → App registrations →
   New registration**.
   - Name: `Ticket to PR (ADO extension)`.
   - **Supported account types: _Accounts in any organizational directory_
     (`AzureADMultipleOrgs`)** — multi-tenant, even for a single-tenant organization. Under nested
     app authentication the Azure DevOps host frame brokers the token on `/common` and **ignores
     the child MSAL client's `authority`**, so a single-tenant registration fails with
     `AADSTS50194` and no page setting can rescue it. The `entraTenantId` setting is still correct
     and still required — it is the authority on any host that is not brokering, which today means
     the dev harness — but it does not make a single-tenant app work inside Azure DevOps.
   - **Redirect URI: platform `Single-page application`, value
     `https://dev.azure.com/_public/_MsalPopup`.**
     This is the redirect the Azure DevOps Nested App Authentication bridge uses on behalf of
     extensions; the extension does not set `redirectUri` itself.
2. Under **Authentication**, confirm the SPA platform is listed and that no client secret exists
   under **Certificates & secrets**. Authorization code flow with PKCE is the default for the SPA
   platform and is what MSAL uses.
3. Under **API permissions**, add delegated **`user_impersonation`**
   (`1a7925b5-f871-417a-9b8b-303f9f29fa10`) on resource application
   **`18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`** — *Azure Machine Learning Services*, which is the
   first-party app that serves the `https://ai.azure.com` audience. It is not discoverable by
   searching for anything you would think to type; search the portal by that GUID. Without it,
   every token request fails with `AADSTS650057: Invalid resource`.
4. **Consent it, and make sure a service principal exists.** Declaring the permission is not
   enough: the page requests `https://ai.azure.com/.default`, and `.default` returns only the
   permissions *already consented to* in the tenant. Without a service principal and an
   `oauth2PermissionGrant` you get the same `AADSTS650057` with the tell-tale
   `List of valid resources from app registration: (empty)`.

   ```bash
   az ad sp create --id <client-id>
   az ad app permission admin-consent --id <client-id>
   ```
5. Note two values for the settings form: the **Application (client) ID** and the **Directory
   (tenant) ID** of *this* registration. The **Agent scope** is not a per-tenant value — it is
   always `https://ai.azure.com/.default` (see below).

Note what step 3 is *not*. It is not a permission on your agent — a Foundry agent exposes none.
It is the delegated scope on the first-party application behind the `https://ai.azure.com`
audience, which is what makes Entra willing to mint the token at all. Authorization to invoke your
particular agent happens afterwards, in Azure RBAC, against the token that permission let you get.

**There is no second app registration for the agent, and you must not create one.** A Foundry
hosted agent does get its own Entra *agent identity* at deploy time, but that identity is what the
agent's container authenticates *outbound* with — it exposes no delegated scope for inbound callers,
and `azd up` registers no `api://` application for it. Who may invoke the agent is decided by Azure
RBAC on the Foundry project or agent, not by an API permission. Anyone using the hub therefore needs
the **Foundry Agent Consumer** role (previously named *Azure AI User*) on the Foundry project or on
the agent, or the agent returns 403. See `docs/deploy.md`.

An earlier revision of this file claimed the agent had "its own app registration, created by
`azd up`" exposing a delegated scope, and the page's validator only accepted `api://…/.default` to
match. Both were wrong: as shipped, the extension could not have obtained a token the agent would
accept. The audience is the Foundry data plane, `https://ai.azure.com/.default`.

### Why nested app authentication

The hub renders in a cross-origin iframe inside `dev.azure.com`, where ordinary popup and hidden-
iframe token flows are unreliable. The page calls `SDK.enableNestedAppAuth()` and then MSAL's
`createNestablePublicClientApplication`, which uses the host frame's auth bridge when it is
available and **falls back to a standard public client automatically** when it is not. Both paths
are public client + PKCE.

---

## Build

```bash
cd extension
npm install
npm run build      # type-checks with tsc, then bundles to dist/
```

`npm run build` produces:

```
dist/hub.html          the page (copied from src/)
dist/hub.js            the bundle (src/main.ts → src/hub.ts + SDK + MSAL)
dist/styles.css        the stylesheet (copied from src/)
dist/agent-config.json administrator-pinned settings (from deploy.local.json; see below)
```

`dist/agent-config.json` is generated, not copied. If `deploy.local.json` exists — written by
`npm run configure`, gitignored — its values are merged in; otherwise the empty tracked template at
`src/agent-config.json` is used and the package ships unpinned.

Other scripts:

| Script | What it does |
|---|---|
| `npm run typecheck` | `tsc --noEmit`, strict |
| `npm run build:dev` | unminified bundle with inline source maps |
| `npm run dev` | the local dev harness (see below) |
| `npm run verify` | headless runtime checks against the real `src/` (see below) |
| `npm run configure` | resolve and record this deployment's publisher id, repository URL and agent settings (`-- --help` for flags) |
| `npm run package` | verify the configuration, build, then produce a `.vsix` in `dist-vsix/` |
| `npm run clean` | remove `dist/`, `dist-vsix/`, `dev/dist/`, `dev/dist-checks/` |

---

## Package and install privately

Nothing tenant-specific is committed here. `vss-extension.json` carries
`REPLACE-WITH-YOUR-PUBLISHER-ID` and `REPLACE-WITH-YOUR-ORG`, and `src/agent-config.json` is empty.
**Do not edit either.** `npm run configure` writes your values into gitignored files instead, so
`git status` is clean after a package and a fork never ships somebody else's identifiers.

1. Configure:

   ```bash
   npm run configure -- \
     --publisher <your-publisher-id> \
     --repo-url  https://github.com/<your-org>/ticket-to-pr-agent \
     --client-id <entra-application-client-id> \
     --tenant-id <entra-directory-tenant-id>
   ```

   Each value is resolved from the flag, then the environment (`T2PR_EXT_PUBLISHER`,
   `T2PR_EXT_REPO_URL`, `T2PR_ENTRA_CLIENT_ID`, `T2PR_ENTRA_TENANT_ID`, `T2PR_AGENT_URL`), then
   `azd env get-values` in the repository root. The agent URL normally needs no flag: it comes from
   `AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` with the `/invocations` suffix and the
   `?api-version=v1` query string stripped. Omit `--client-id`, `--tenant-id` and `--agent-url`
   together to ship an unpinned package where each user configures their own agent; supplying only
   some of them is refused. `--publisher` is always required.

   It writes three gitignored files — `deploy.local.json` (the resolved values),
   `vss-extension.local.json` (the manifest `tfx` packages) and `dist/agent-config.json` — and it
   validates them by bundling `src/host.ts` and calling the same `validateSettings` the settings
   form calls, so anything the page would reject at runtime is rejected here instead.

2. Build and package:

   ```bash
   npm run package
   # → dist-vsix/<publisher>.ticket-to-pr-0.1.1.vsix
   ```

   `npm run package` fails loudly if step 1 has not been run — a package built from the tracked
   placeholders cannot be uploaded anywhere, so producing one silently is worse than failing.

3. Upload it at <https://marketplace.visualstudio.com/manage>, keep it **private** (the manifest
   already sets `"public": false`), and share it with your organization.

4. **Wait ~100 seconds for Marketplace verification.** Until the version's flags read `validated`
   the extension's page 404s and the API reports `ExtensionDoesNotExistException`. That reads like
   a failed upload; it is not. Do not re-upload — it restarts the clock.

5. In your organization: **Organization settings → Extensions → Shared → Install**.

6. The hub appears under **Repos → Ticket to PR**.

To publish from the command line instead, `npx tfx extension publish --manifest-globs
vss-extension.local.json --share-with <your-org>` with a personal access token that has the
**Marketplace (manage)** scope — note the *generated* manifest, not the tracked placeholder one.
That PAT is for publishing from your own machine only; it is never part of the package.

### Manifest facts

| Field | Value | Why |
|---|---|---|
| `targets` | `Microsoft.VisualStudio.Services` | Azure DevOps Services and Server 2019+ |
| `categories` | `Azure Repos` | one of the five valid values |
| `contributions[0].type` | `ms.vss-web.hub` | a hub page |
| `contributions[0].targets` | `ms.vss-code-web.code-hub-group` | the **Repos** hub group |
| `scopes` | `vso.work`, `vso.code`, `vso.extension.data_write` | read work items, read repositories, read/write the caller's own (User-scoped) settings document |

The scopes are read-only against work items and code. The extension never writes to Azure DevOps —
the agent does that with its own identity.

---

## Configure the extension

Three values decide which agent this page talks to and how it signs in, plus one constant — the
audience it asks Entra for a token for. They can come from either of two places.

| Field | Example | Rule enforced in the page |
|---|---|---|
| Agent Application URL | `https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols` | `https`, host ending in `.services.ai.azure.com`, no username/password, no query string, no fragment |
| Entra application (client) ID | the SPA registration from above | must be a GUID |
| Entra directory (tenant) ID | your directory | must be a GUID |
| Agent scope | `https://ai.azure.com/.default` | must be exactly that string — see below |

The URL is the agent's Foundry endpoint with `/invocations` **and any query string** removed,
because the page appends `/invocations`, `/invocations/{runId}` and `?api-version=v1` itself. `azd`
writes the full endpoint into the active environment as
`AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT`, and `azd ai agent show` prints it; `npm run configure`
strips both for you.

`api-version=v1` is not optional and is not configurable. Every call to the Foundry gateway carries
it — `agentEndpoint()` in `src/host.ts` — because without it the gateway answers **every** request,
POST and polling GET alike, with `400 BadRequest: Missing required query parameter: api-version`
before the agent container is reached. That is also why a configured URL may not carry its own query
string: two sources of query parameters on one URL is how you get a client that works until it
doesn't.

Anything that fails those rules is refused by the form *and* discarded when read back from storage,
before any token is requested.

### Recommended: pin them in the package (administrator-controlled)

Pass the values to `npm run configure` before packaging. They land in the gitignored
`deploy.local.json`, and `npm run build` merges them into `dist/agent-config.json`, which the hub
reads at startup:

```jsonc
// dist/agent-config.json — generated, gitignored, never hand-edited
{
  "default": {
    "agentUrl": "https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols",
    "entraClientId": "00000000-0000-0000-0000-000000000000",
    "entraTenantId": "00000000-0000-0000-0000-000000000000",
    "agentScope": "https://ai.azure.com/.default"
  },
  "projects": {
    "Payments": {
      "agentUrl": "https://contoso.services.ai.azure.com/api/projects/payments/agents/ticket-to-pr/endpoint/protocols"
    }
  }
}
```

`default` applies everywhere; a `projects` entry (matched on project name, case-insensitively)
overrides it for that project — add one with `--project "Payments=https://…/endpoint/protocols"`.
When the file yields a complete, valid set of values the hub uses them for **everyone in the
organization** and the settings form is shown read-only. A half-filled set is ignored rather than
half-used, which is why `configure` refuses to write one.

The tracked template at `src/agent-config.json` is deliberately **empty** and stays that way. This
is a public repository; one tenant's agent URL and app registration are not a default anybody else
wants, and hand-editing that file is exactly how a fork ends up shipping them.

Changing the values means re-running `configure`, repackaging and reinstalling the extension, which
requires the organization's **Manage extensions** permission. That permission is the access control
on these values — Azure DevOps does not offer one for extension settings documents (see
[Known limitations](#known-limitations)).

### Fallback: per-user settings

If the package carries no pinned settings (the default, and what you get when `configure` is run
without `--client-id`/`--tenant-id`/`--agent-url`), each user configures the extension for themselves:
open the hub, expand **Agent settings**, fill the fields in, save. The document is written at
the extension data service's **User** scope, so it is readable and writable only by its owner and
one user cannot change what another user's page does. Until they are all set the banner stays up
and **Create PR with agent** is disabled.

### Why the scope is one literal and the URL is pinned to a Foundry host

Whatever writes these settings chooses the audience of the token this page hands to the agent URL,
*and* the host it is handed to. Both are therefore allowlists of exact values, not patterns.

**The scope.** `validateAgentScope` compares against a one-entry list of literals
(`ALLOWED_AGENT_SCOPES` in `src/host.ts`). It is deliberately not a regular expression: a pattern
loose enough to admit `https://ai.azure.com/.default` — anything shaped like `https://…/.default` —
would equally admit `https://graph.microsoft.com/.default`, `https://management.azure.com/.default`
and `https://vault.azure.net/.default`, and the page would fetch a token for whichever the settings
named and POST it to the configured host. A literal cannot be widened by accident. The comparison is
exact and case-sensitive, so `.DEFAULT`, a trailing slash, and a second scope smuggled in after a
space or a newline are all refused.

**The URL.** `https://ai.azure.com` is a **shared, first-party audience**. The token minted for it
is not scoped to this agent — it is the signed-in user's Foundry data-plane token, accepted by every
Foundry project and agent that user can reach. So unlike a private `api://` audience, sending it to
the wrong host is not a dead credential; it is the user's whole Foundry access, handed over. That
makes `validateAgentUrl` load-bearing in a way it was not when the audience was per-agent, and it is
no longer enough for it to check only the URL's *shape*.

It therefore also pins the **host**: the hostname must end in `.services.ai.azure.com`, which is the
shape of a Foundry project endpoint. Two consequences follow. The token can only ever be sent to a
Microsoft-operated Foundry gateway — never to an attacker's own server. And because that gateway
does not forward the `Authorization` header to the container behind it (`docs/contract.md` §2), even
a URL naming somebody *else's* Foundry account leaks nothing to their agent code. `hostname` is
already lower-cased and punycoded by the URL parser, so a homograph host cannot match the suffix by
looking like it, and `evilservices.ai.azure.com` (no dot) and `x.services.ai.azure.com.evil.test`
(suffix in the middle) are both refused.

The cost is real and is stated here rather than buried: this build talks to Foundry in the Azure
public cloud only. A sovereign cloud uses a different endpoint suffix *and* a different audience, so
it needs both constants in `src/host.ts` changed and the extension rebuilt. That is a deliberate
trade — a code change reviewed in a pull request, rather than a text field that can send a
first-party token anywhere.

`dev/checks/checks-audience.mjs` holds the corpus that proves it: 25 adversarial scope strings and
11 adversarial URLs, including every audience security review named — Graph, ARM, the bare Azure
DevOps resource GUID `499b84ac-…`, `app.vssps.visualstudio.com`, Key Vault, space- and
newline-separated multi-scope smuggling, and `.DEFAULT` case variants.

---

## Local dev harness

You can see and exercise the whole UI on a laptop with **no Azure DevOps organization and no
deployed agent**:

```bash
cd extension
npm install
npm run dev
# → http://127.0.0.1:5173/
```

This runs the real `src/hub.ts`, `src/hub.html` and `src/styles.css`. Only the seam is replaced:
`src/host.ts` defines a `HubHost` interface covering every call that leaves the page, `src/ado-host.ts`
implements it over the ADO SDK, MSAL and `fetch`, and `dev/stub-host.ts` implements the same
interface against in-memory fixtures. `dev/main.ts` is a four-line mirror of `src/main.ts` that
passes the stub instead of the real host. `dev/dist/hub.html` is *generated* from `src/hub.html` on
every build with its script tag rewritten, so there is no second copy of the markup to drift.

The harness page puts the hub in an iframe with a control panel beside it:

- **Scenario** — succeed, any of the eleven contract error codes, or an unreachable agent. Each
  error fires at the phase it would really fire at (`clone_failed` during cloning, `pr_failed`
  during PR creation, and so on), so you can confirm every code renders as a sentence.
- **Timing** — milliseconds per phase and simulated API latency. The hub still polls on the real
  5-second interval; shortening the phases just makes a run finish in one or two polls.
- **Host theme** — light or dark, applied exactly as the ADO host applies it (palette variables
  written onto `:root`), so both themes are exercised for real.
- **Data states** — work items and repositories can be normal, empty, or failing to load; project
  settings can be configured or not. Covers the loading, empty, error and unconfigured states.
- **Log** — every host call the hub makes, including the full request body posted to `/invocations`.

Two things the harness checks for you:

- The stub **rejects any request body containing a field the contract does not define**, and any
  branch name that fails `^[A-Za-z0-9._/-]+$`, exactly as the real server does. A regression that
  adds a `harness` or `model` field fails loudly here.
- Fixture work items **#4131** and **#4140** carry a script payload and HTML entities in their
  titles. They must appear as literal text in the picker. Nothing in `src/` uses `innerHTML`;
  everything goes into the DOM through `textContent`.

Work item **#9001** is deliberately absent from the list — type `9001` in the search box and press
Enter to exercise lookup-by-ID.

The **Project settings** control has a third position, *Pinned by the package*, which loads the hub
with managed settings so the read-only form can be seen.

Nothing under `dev/` is packaged: `vss-extension.json` ships only `dist/` and `images/`.

### Headless runtime checks

```bash
npm run verify
```

`scripts/verify.mjs` bundles `dev/checks/` together with the real `src/hub.ts` and
`src/ado-host.ts` for Node, aliasing `azure-devops-extension-sdk` and `@azure/msal-browser` to
recording stubs and replacing `fetch`. `src/` is unmodified and unaware. `dev/checks/dom-stub.mjs`
implements only the DOM surface `src/dom.ts` uses, and seeds its elements from the **real**
`src/hub.html`, so a check fails if the markup and the code disagree about an id.

The 24 checks are behavioural, not unit tests of helpers — they drive `bootstrap()` and the real
poll loop on real timers. They cover: a 403 mid-poll stopping the loop after one request; a 5xx
being retried and the five-in-a-row cap ending it; two and three submits leaving exactly one poll
chain; polling never reaching `acquireTokenPopup`; a hostile URL or scope in storage being
discarded before a token is requested; settings never touching the organization-wide scope;
malformed `202` and status bodies being rejected instead of rendered; and a non-`https` pull
request URL never becoming an `href`. They take about a minute, most of it real five-second poll
intervals.

`npm run verify` is a development aid, not the shipped test suite — that is the agent's
deliverable.

---

## Project layout

```
extension/
├── vss-extension.json     manifest, tracked — placeholder publisher id and links
├── vss-extension.local.json  generated by configure, gitignored — what tfx packages
├── deploy.local.json      generated by configure, gitignored — this deployment's values
├── package.json
├── tsconfig.json
├── scripts/
│   ├── configure.mjs      resolves publisher/agent/Entra values; writes the two .local files
│   ├── build.mjs          esbuild production bundle
│   ├── dev-server.mjs     dev harness build + static server
│   ├── verify.mjs         runs the runtime checks in dev/checks
│   ├── clean.mjs
│   └── make-icon.mjs      regenerates images/logo.png
├── images/logo.png
├── src/
│   ├── hub.html           the page
│   ├── hub.ts             all UI logic; exports bootstrap(host)
│   ├── styles.css         themed entirely from host palette variables
│   ├── main.ts            production entry: bootstrap(createAdoHost())
│   ├── host.ts            the HubHost seam + settings helpers
│   ├── ado-host.ts        real host: ADO SDK, MSAL, agent HTTP
│   ├── contract.ts        transcription of the published contract
│   ├── errors.ts          the eleven error codes as sentences
│   ├── branch.ts          branch name suggestion + validation
│   ├── dom.ts             escaping DOM helpers (no innerHTML)
│   └── agent-config.json  tracked template for the packaged settings — deliberately empty
└── dev/                   local harness — never packaged
    ├── index.html         control panel + hub iframe
    ├── main.ts            dev entry: bootstrap(new StubHost(...))
    ├── stub-host.ts       fixtures + simulated agent
    ├── themes.ts          ADO light/dark palettes
    └── checks/            headless runtime checks (npm run verify)
        ├── dom-stub.mjs   the DOM that src/hub.ts actually uses, seeded from hub.html
        ├── sdk-stub.mjs   azure-devops-extension-sdk stand-in, records every call
        ├── msal-stub.mjs  @azure/msal-browser stand-in, counts interactive calls
        ├── checks-host.mjs, checks-hub.mjs, checks-audience.mjs, harness.mjs, main.mjs
```

---

## Security notes

- **No secrets in the package.** MSAL is configured as a public client with PKCE. There is no
  client secret, API key or PAT in `src/`, in the manifest, or in the built bundle.
- **Tokens never reach the UI layer.** The `HubHost` interface has no `getToken()`. Tokens are
  acquired and attached to requests entirely inside `ado-host.ts`; `hub.ts` cannot log, render or
  leak one. Nothing in `src/` calls `console.*`.
- **No `innerHTML`.** Work item titles, repository names and agent error messages are attacker-
  influenceable text and are only ever written as text nodes (`src/dom.ts`).
- **The agent URL must be `https`, on a Foundry host, with no userinfo, query string or fragment.**
  The page sends a bearer token to it, and that token is for the *shared* `https://ai.azure.com`
  audience — the user's whole Foundry data-plane access, not a credential private to this agent. So
  the hostname is pinned to `*.services.ai.azure.com`: the token can reach a Microsoft-operated
  Foundry gateway and nothing else, and that gateway does not pass the `Authorization` header to the
  container behind it. `https://agent.example@evil.com/` resolves to `evil.com`, so userinfo is
  refused outright rather than displayed and trusted. The check runs in three places: the settings
  form, `loadSettings` (a stored value that would fail the form is discarded), and again at the top
  of `agentFetch` **before** the token is acquired — so no code path can send a token to a URL that
  has not been through it.
- **The token audience is a one-entry literal allowlist.** `agentScope` must be exactly
  `https://ai.azure.com/.default`. It is compared as a string, not matched against a pattern,
  precisely so that no future edit can widen it into `https://…/.default` and re-admit Microsoft
  Graph, ARM, Key Vault or the Azure DevOps resource as the audience of the token this page gives
  away.
- **Nothing interactive happens on the polling path.** Token acquisition while a run is being
  followed is silent-only. If the silent path cannot produce a token the poll stops and says so,
  instead of raising a sign-in popup every five seconds for as long as the run lasts.
- **Polling stops on a permanent failure.** A 401, 403, 404 or an unparseable body ends the loop
  after one request with the reason on screen; only a transport failure, a 5xx, a 408 or a 429 is
  retried, and only five times in a row. Submitting again cancels the previous run's poll chain, so
  the request rate cannot compound.
- **Agent responses are shape-checked before they reach the DOM.** `runId` must be a non-empty
  string, `status` one of the four contract values, `phase` one of the six; a `result` or `error`
  object must carry every field the contract defines. Anything else is an error message, not a
  half-rendered panel. The pull request link is only ever created for an `https` URL — an `href`
  is also a middle-click and a "copy link address", neither of which runs the click handler.
- Branch names are validated client-side against the contract's `^[A-Za-z0-9._/-]+$`, plus a few
  extra rules for refs git itself would reject (leading/trailing `/`, `//`, `..`, trailing `.lock`).
  The server stays the authority — `branch_exists` in particular can only be detected there, and is
  handled in the status panel with a prompt to change the name.

---

## Known limitations

These are called out for review rather than hidden:

1. **Azure DevOps has no project-scoped, access-controlled settings store, so the intended
   "project-level settings" is met in two halves.** The extension data service offers exactly two
   scopes, `Default` (the whole organization) and `User`, and no per-document ACL. Every user of
   the hub holds `vso.extension.data_write`, so a document at `Default` scope is writable by any
   member of the organization — and since these settings name both the host the page sends an Entra
   token to and the audience it requests that token for, an organization-wide writable document is
   a token-redirection primitive, not a preference. This extension therefore **never reads or
   writes the `Default` scope**. Settings come from the packaged `dist/agent-config.json`
   (administrator-controlled: changing it requires the organization's Manage extensions permission)
   or from a `User`-scoped document that only its owner can read or write.

   Residual exposure, stated plainly:

   - A user can still point **their own** page at a different agent, and that page will send
     **their own** token there. The blast radius is bounded by the host allowlist: the URL must be
     a `*.services.ai.azure.com` Foundry endpoint, so the worst case is the user's Foundry token
     reaching a Microsoft-operated gateway for somebody else's Foundry account — which does not
     forward the `Authorization` header to that account's container. It cannot be sent to a host
     the attacker controls. Nothing a user does affects another user's page.
   - The packaged file is only as trustworthy as the package. Whoever can install extensions in the
     organization can already run arbitrary script in the hub, so this adds no new authority.
   - Per-project pinning is by **project name**, not project id, because the packaged file is
     written before any project id is known. Renaming a project silently falls back to the
     `default` block rather than failing loudly.
   - There is no project-administrator check on the per-user save, because at `User` scope there is
     nothing to gate: a user is authorised to configure their own page. A genuine project-admin
     gate would need the Azure DevOps permissions API, whose documented scope is
     `vso.security_manage` — asking every user of the hub to grant the extension management of
     security permissions is a worse trade than the risk it would close.
2. **`baseBranch` is a free-text field.** It is not validated against the repository's actual
   branches; a wrong value surfaces as `clone_failed` from the agent.
3. **Recent work items are organization-wide.** `accountmyworkrecentactivity` is an organization-
   scoped endpoint, so the list is filtered to the current project client-side.
4. **Node 20+ only for the build.** The runtime bundle targets ES2022 and the browsers Azure DevOps
   supports; the build scripts use modern Node APIs.

---

## What was verified

The extension has since been packaged, installed and used in a **live Azure DevOps organization**
against a deployed agent. What that established, each stated with the failure it produces when it
is wrong:

| Fact | Failure without it |
|---|---|
| **Every gateway call carries `?api-version=v1`** — POST and polling GET alike. `agentEndpoint()` adds it; there is nothing to configure | `400 BadRequest: Missing required query parameter: api-version`, before the agent container is reached |
| **The app registration must be `AzureADMultipleOrgs`.** Under nested app auth the Azure DevOps host frame brokers on `/common` and ignores the child MSAL client's `authority`, so `entraTenantId` — correct and required for non-brokering hosts — cannot rescue a single-tenant app inside Azure DevOps | `AADSTS50194: … Usage of the /common endpoint is not supported for such applications` |
| **Delegated `user_impersonation` (`1a7925b5-f871-417a-9b8b-303f9f29fa10`) on resource app `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`** — *Azure Machine Learning Services*, the first-party app serving the `https://ai.azure.com` audience. Not findable by search; only the failure names it | `AADSTS650057: Invalid resource` |
| **A service principal and an `oauth2PermissionGrant`, not just the declaration.** `.default` returns only already-consented permissions, so an unconsented declaration yields an empty resource list | `AADSTS650057` again, with `List of valid resources from app registration: (empty)` |
| **The Agent Application URL is the endpoint with `/invocations` and the query string removed** | `…/invocations/invocations` 404, or a rejected URL in the settings form |
| **Marketplace verification takes ~100 seconds**; the extension does not exist until the version's flags read `validated` | a 404 page and `ExtensionDoesNotExistException` from the API, which read like a failed upload |

`docs/extension.md` [step 2](../docs/extension.md#2-register-the-entra-application) has the `az`
commands for each of the registration facts.

### Checked against current documentation while building

Rather than assumed:

| Thing | Result |
|---|---|
| `azure-devops-extension-sdk` latest | 5.0.0; `init`, `ready`, `notifyLoadSucceeded`, `getHost`, `getUser`, `getWebContext`, `getExtensionContext`, `getService`, `getAccessToken`, `enableNestedAppAuth`, `applyTheme`, `resize` all confirmed in the installed `SDK.d.ts` |
| `SDK.init({ applyTheme: true })` | confirmed in `SDK.ts`: writes `--<name>: <value>` onto `:root` and sets `body { color: var(--text-primary-color) }` |
| Theme variable names | taken from the Azure DevOps dark palette; `--palette-neutral-0` is the background end and `--palette-neutral-100` the text end in both themes |
| Hub group id for Repos | `ms.vss-code-web.code-hub-group`, contribution type `ms.vss-web.hub` |
| Manifest required fields / categories / targets | `manifestVersion`, `id`, `version`, `name`, `publisher`, `categories`, `targets`; categories must be one of the five documented values; `Microsoft.VisualStudio.Services` is the Azure DevOps target |
| Scope strings | `vso.work`, `vso.code`, `vso.extension.data_write` |
| `@azure/msal-browser` | 5.21.0; `createNestablePublicClientApplication` confirmed in the installed `types/app/PublicClientApplication.d.ts`; documented to fall back to a standard public client when the NAA bridge is unavailable |
| NAA redirect URI | `https://dev.azure.com/_public/_MsalPopup`, and the extension must not set `redirectUri` itself |
| ADO REST 7.1 endpoints | `POST {project}/_apis/wit/wiql` (with `$top`), `GET {project}/_apis/wit/workitems?ids=&fields=`, `GET {project}/_apis/git/repositories`, `GET _apis/work/accountmyworkrecentactivity` — URIs and response field names confirmed against the REST reference |
| Extension data service | contribution id `ms.vss-features.extension-data-service`; `IDocumentOptions.scopeType` is only `Default` or `User` — there is no project scope and no per-document ACL, which is why settings are `User` scoped with the key namespaced by project id, and why the organization-wide option for them is the extension package itself |
| **Token audience for a Foundry hosted agent** | `https://ai.azure.com/.default`. *Authentication and authorization in Microsoft Foundry* states "Microsoft Entra ID uses OAuth 2.0 bearer tokens scoped to `https://ai.azure.com/.default`", and lists as the fix for a 401 "Verify the token acquisition scope is `https://ai.azure.com/.default`". *Deploy a hosted agent* shows the Invocations protocol called with `credential.get_token("https://ai.azure.com/.default")` (Python), `credential.getToken("https://ai.azure.com/.default")` (JS) and `az account get-access-token --resource https://ai.azure.com` (REST) |
| **No `api://` app registration for the agent** | *Hosted agents in Foundry Agent Service*: the platform "assigns a dedicated Microsoft Entra ID (agent identity)", which is "the identity the agent container authenticates with at runtime … for model invocation, tool access, and downstream Azure services" — an outbound identity. *Agent applications* is explicit that "Agent Application RBAC is managed through Azure Resource Manager, not through the Entra agent identity. The Entra agent identity that the published agent receives is for the agent's *own* outbound calls." Nothing creates an application exposing a delegated scope for inbound callers |
| **Agent endpoint host shape** | `{project_endpoint}/agents/{name}/endpoint/protocols/invocations`, where the project endpoint is `https://{account}.services.ai.azure.com/api/projects/{project}` (*Hosted agents in Foundry Agent Service*, "Agent identity and endpoint") — the basis for pinning the URL host to `.services.ai.azure.com` |
| **Caller's role** | **Foundry Agent Consumer** (previously *Azure AI User*) on the Foundry project or agent for a hosted-agent endpoint; note that *Agent applications* warns Foundry Agent Consumer does **not** cover a published Agent Application, which needs Foundry User instead |

### Still not verified

- The light-theme palette values in `dev/themes.ts` are the standard ADO light ramp but are a close
  approximation, not a byte-for-byte copy. They affect the **dev harness only** — in a real
  organization the host supplies the authoritative values.
- **A full run has not been watched through to a pull request link in the panel.** Submission and
  polling work against the live gateway; a long coding run completing and the success state
  rendering a live PR has not been observed end to end.
- **Per-user consent.** The live installation uses tenant-wide admin consent
  (`consentType: AllPrincipals`). Whether a tenant that permits user consent produces the grant
  cleanly at first sign-in inside the brokered NAA popup — which cannot show an arbitrary consent
  screen — has not been tested.
- **Sovereign clouds and Azure DevOps Server.** The host suffix and the audience are compiled-in
  constants; a sovereign cloud needs both changed and a rebuild. Nothing has been run against an
  on-premises collection.
