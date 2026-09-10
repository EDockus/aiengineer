# The Azure DevOps extension — package, install, configure, use

This page is for the person who **installs the extension into an Azure DevOps organization** and
the people who use it afterwards. It covers packaging with `tfx`, installing privately from the
Visual Studio Marketplace, the Entra app registration it needs, the two ways to configure it,
and what to do when it does not work.

If you are changing the extension's code, read [`extension/README.md`](../extension/README.md)
instead — that is the developer-facing page (project layout, the dev harness, the headless
checks, the reasoning behind each validation rule). The two pages point at each other rather than
repeating each other.

---

## Contents

- [What it is](#what-it-is)
- [Before you start](#before-you-start)
- [1. Create a publisher](#1-create-a-publisher)
- [2. Register the Entra application](#2-register-the-entra-application)
  - [2.1 Create it](#21-create-it)
  - [2.2 It must be multi-tenant, even for a single-tenant organization](#22-it-must-be-multi-tenant-even-for-a-single-tenant-organization)
  - [2.3 It needs `user_impersonation` on Azure Machine Learning Services](#23-it-needs-user_impersonation-on-azure-machine-learning-services)
  - [2.4 It needs a service principal *and* a consent grant](#24-it-needs-a-service-principal-and-a-consent-grant)
  - [2.5 Do not create a second registration for the agent](#25-do-not-create-a-second-registration-for-the-agent)
- [3. Configure, build and package](#3-configure-build-and-package)
  - [Calling it from a script](#calling-it-from-a-script)
- [4. Install privately](#4-install-privately)
- [5. Configure it](#5-configure-it)
  - [The Agent Application URL — what exactly to paste](#the-agent-application-url--what-exactly-to-paste)
  - [`api-version` is required on every call](#api-version-is-required-on-every-call)
  - [The audience](#the-audience)
  - [Option A: pin it in the package](#option-a-pin-it-in-the-package)
  - [Option B: per-user settings](#option-b-per-user-settings)
  - [Which one to use](#which-one-to-use)
- [6. Using it](#6-using-it)
- [7. Troubleshooting](#7-troubleshooting)
- [8. Known unverified](#8-known-unverified)
- [9. Updating and uninstalling](#9-updating-and-uninstalling)
- [Related](#related)

---

## What it is

A single hub page contributed under **Repos** in Azure DevOps, called **Ticket to PR**. A user
picks a work item, picks a repository, accepts or edits the suggested branch name, and presses
**Create PR with agent**. The page submits one request to the
[ticket-to-pr agent](contract.md), polls it every five seconds while it works, shows the phase the
run is in, and links to the pull request when it is done.

It is a static page. There is no backend and no server component inside the package. It talks to
Azure DevOps with the extension SDK's own token, and to the agent with an Entra token for the
**signed-in user** — so a run is attributable to the person who started it, and someone with no
access to the agent cannot start one.

The agent it calls is a separate deployment. Install this after
[`docs/deploy.md`](deploy.md) has produced a working agent, not before.

For the request/response flow in detail see [`docs/contract.md`](contract.md); for where the
extension sits in the system see [`docs/architecture.md`](architecture.md).

---

## Before you start

| You need | Notes |
|---|---|
| A **deployed agent**, and its endpoint | From [`docs/deploy.md`](deploy.md). `azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` prints it, and so does `azd ai agent show`. |
| The **Foundry Agent Consumer** role for everyone who will use the hub | Invocation access is Azure RBAC on the Foundry project or agent — see [deploy.md §5](deploy.md#5-let-callers-invoke-the-agent). Without it the agent returns 403 no matter how the extension is configured. Grant it to an Entra group once rather than per person. |
| **Node.js 20 or newer** with npm | Only to build and package. Nothing Node-related ships in the extension or runs in the browser build. Developed against Node 23 / npm 11. |
| A **Visual Studio Marketplace publisher** | Free. Created at <https://marketplace.visualstudio.com/manage>. A private extension is visible only to the organizations you share it with. |
| **`tfx-cli`** | Already a devDependency of `extension/package.json`, so `npm ci` installs it and `npx tfx` runs that local copy. No global install needed. |
| **`azd` with the agent's environment selected** (optional) | `npm run configure` reads the agent endpoint straight out of `azd env get-values`, so you do not have to paste it. Without azd, pass `--agent-url` explicitly. |
| **Manage extensions** permission in the target Azure DevOps organization | Needed to install the extension, and — if you pin the settings in the package — it is also the access control on those settings. |
| An **Entra app registration** for the page | One public-client SPA registration, **multi-tenant**, with a consented `user_impersonation` grant on Azure Machine Learning Services. Four non-obvious requirements, each with the error it produces when missing: [step 2](#2-register-the-entra-application). |

This build talks to Microsoft Foundry in the **Azure public cloud only**. The page pins the agent
host to `*.services.ai.azure.com` and the token audience to `https://ai.azure.com/.default`. A
sovereign cloud uses a different endpoint suffix *and* a different audience, so it needs both
constants in `extension/src/host.ts` changed and the extension rebuilt — a reviewed code change,
deliberately, rather than a text box that could send a first-party token anywhere. The reasoning
is in [`extension/README.md`](../extension/README.md#configure-the-extension).

---

## 1. Create a publisher

`extension/vss-extension.json` is tracked, and it ships with placeholders rather than anybody's real
identifiers:

```json
"publisher": "REPLACE-WITH-YOUR-PUBLISHER-ID",
...
"uri": "https://github.com/REPLACE-WITH-YOUR-ORG/ticket-to-pr-agent"
```

**Do not edit that file.** `npm run configure` ([step 3](#3-configure-build-and-package)) reads your
publisher id and repository URL and writes them into an untracked
`extension/vss-extension.local.json`, which is the manifest `tfx` actually packages. The tracked file
stays clean, so a fork never carries one deployment's marketplace account into someone else's.

1. Sign in at <https://marketplace.visualstudio.com/manage> with the account that will own the
   extension, and create a publisher. The **publisher ID** is the short, immutable identifier (for
   example `contoso-devtools`), not the display name. `tfx` names the VSIX
   `<publisher>.<id>-<version>.vsix`, and the Marketplace rejects an upload whose publisher id you
   do not own.
2. Decide the repository URL for the **Repository** and **Support** links on the extension's
   Marketplace page — your fork's, or this repository's if you are installing it unchanged. You pass
   it as `--repo-url`.

Two more manifest facts worth knowing before you upload:

- `"public": false` is already set. Leave it. That is what makes the extension private.
- `content.details.path` is `README.md`, so **`extension/README.md` becomes the extension's
  Marketplace description page**. If you fork and want a different description there, edit that
  file — its relative links will not resolve on the Marketplace, so keep anything essential
  self-contained.

The extension requests three Azure DevOps scopes: `vso.work` and `vso.code` (read work items and
repositories) and `vso.extension.data_write` (read and write the caller's own settings document).
All read-only against your work items and code — the extension never writes to Azure DevOps; the
agent does that, with its own identity.

---

## 2. Register the Entra application

The page signs the user in and asks Entra for a token for the Foundry data plane. That needs one
app registration — a **public client with no secret** — and four specific things about it. Each of
the four fails in a way that names something other than its real cause, so they are stated here as
facts with the error they produce when they are wrong. All four were established against a live
Azure DevOps organization; none of it is inference.

### 2.1 Create it

Azure portal → **Microsoft Entra ID → App registrations → New registration**:

- Name: something recognisable, e.g. `Ticket to PR (ADO extension)`.
- **Supported account types: _Accounts in any organizational directory_** (`AzureADMultipleOrgs`).
  Not single tenant, even if your organization is. Why: [2.2](#22-it-must-be-multi-tenant-even-for-a-single-tenant-organization).
- **Redirect URI: platform `Single-page application`, value
  `https://dev.azure.com/_public/_MsalPopup`** — the redirect used by the Azure DevOps nested app
  authentication bridge. The extension does not set `redirectUri` itself.

Then confirm under **Certificates & secrets** that there is **no client secret**. There must never
be one: the package is downloadable by anyone who can install it, so a secret in it is a published
secret. MSAL runs here as a public client with PKCE.

Note the **Application (client) ID** and the **Directory (tenant) ID** — they are two of the values
you pass to `npm run configure` in [step 3](#3-configure-build-and-package).

### 2.2 It must be multi-tenant, even for a single-tenant organization

`signInAudience` must be `AzureADMultipleOrgs`. A single-tenant registration fails at sign-in:

```
AADSTS50194: Application '<client-id>' is not configured as a multi-tenant application.
Usage of the /common endpoint is not supported for such applications created after '10/15/2018'.
Use a tenant-specific endpoint or configure the application to be multi-tenant.
```

The obvious reading of that message — "so name the tenant instead of using `/common`" — does not
work here, and it is worth knowing why before you spend an afternoon on it. The hub runs inside the
Azure DevOps host frame under **nested app authentication (NAA)**. The MSAL client in the page does
not perform the sign-in itself; it asks the host frame to broker a token. **The host brokers on
`/common` and ignores the `authority` the child client was configured with.** There is no setting
in the page that changes the endpoint the request actually reaches.

So the `entraTenantId` setting is not the fix for this. It is still correct and still required —
`entraAuthority()` in `src/host.ts` builds `https://login.microsoftonline.com/{tenant}` from it, and
that is the authority used on any host that is *not* brokering, which today means the local dev
harness. It confines sign-in to one directory there rather than to any tenant that happens to hold a
matching app id. Both things are true at once: **name the tenant, and mark the app multi-tenant.**

```bash
az ad app update --id <client-id> --sign-in-audience AzureADMultipleOrgs
```

Multi-tenant here does not mean other tenants can use your agent. No other tenant has a service
principal for the registration ([2.4](#24-it-needs-a-service-principal-and-a-consent-grant)), and
invocation is gated by Azure RBAC on the Foundry project regardless
([deploy.md §5](deploy.md#5-let-callers-invoke-the-agent)).

### 2.3 It needs `user_impersonation` on Azure Machine Learning Services

The registration needs exactly one delegated permission:

| | |
|---|---|
| Resource application id | `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe` |
| Resource display name | **Azure Machine Learning Services** |
| Permission | `user_impersonation`, delegated, id `1a7925b5-f871-417a-9b8b-303f9f29fa10` |

That first-party application is what serves the `https://ai.azure.com` audience. **You will not find
it by searching.** It does not appear under a name resembling Foundry, `ai.azure.com` or AI Services
in the portal's *APIs my organization uses* picker, and no Foundry documentation names it. The only
thing that identifies it is the failure you get without it:

```
AADSTS650057: Invalid resource. The client has requested access to a resource which is not listed
in the requested permissions in the client's application registration.
```

Add it by GUID, not by name:

```bash
az ad app permission add --id <client-id> \
  --api 18a66f5f-dbdf-4c17-9dd7-1634712a9cbe \
  --api-permissions 1a7925b5-f871-417a-9b8b-303f9f29fa10=Scope
```

In the portal: **API permissions → Add a permission → APIs my organization uses**, paste
`18a66f5f-dbdf-4c17-9dd7-1634712a9cbe` into the search box — the GUID matches even though none of
the names you would think to type do — then **Delegated permissions → user_impersonation**.

### 2.4 It needs a service principal *and* a consent grant

This is the one that costs the most time: **declaring the permission is not enough.**

The page requests `https://ai.azure.com/.default`. `.default` does not mean "everything the
registration declares". It means **"everything that has already been consented to for this client in
this tenant"**. With the permission declared but never consented, that set is empty, and Entra
returns the same error code as [2.3](#23-it-needs-user_impersonation-on-azure-machine-learning-services)
with one extra line that gives it away:

```
AADSTS650057: Invalid resource. ...
List of valid resources from app registration: (empty)
```

`(empty)` is the signature. The resource list is built from **grants**, not from declarations, so two
directory objects have to exist:

1. a **service principal** for the registration in the tenant, and
2. an **`oauth2PermissionGrant`** from that service principal to the Azure Machine Learning Services
   service principal, carrying the `user_impersonation` scope.

Admin consent creates both:

```bash
az ad sp create --id <client-id>                 # no-op if it already exists
az ad app permission admin-consent --id <client-id>
```

Confirm you are in the state a working installation is in:

```bash
az ad app show --id <client-id> \
  --query "{audience:signInAudience, permissions:requiredResourceAccess}"
# audience -> "AzureADMultipleOrgs"
# resourceAppId -> "18a66f5f-dbdf-4c17-9dd7-1634712a9cbe", resourceAccess id -> "1a7925b5-..."

SP_ID=$(az ad sp show --id <client-id> --query id -o tsv)
az rest --method get \
  --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '$SP_ID'" \
  --query "value[].{consentType:consentType, scope:scope}"
# [ { "consentType": "AllPrincipals", "scope": "user_impersonation" } ]
```

`AllPrincipals` is tenant-wide admin consent: every user is covered and nobody sees a consent prompt.
`Principal` — per-user consent, created at each user's own first sign-in — also works, if your tenant
lets users consent for themselves. What does not work is no grant at all.

### 2.5 Do not create a second registration for the agent

**Do not create an app registration for the agent, and do not look for one.** A Foundry hosted agent
is given an Entra *agent identity* at deploy time, but that is the identity the agent's own container
authenticates **outbound** with. It exposes no delegated scope for inbound callers, and `azd up`
registers no `api://` application for it. Who may invoke the agent is decided by **Azure RBAC** — the
**Foundry Agent Consumer** role, [deploy.md §5](deploy.md#5-let-callers-invoke-the-agent) — not by an
API permission on an app registration.

The permission in [2.3](#23-it-needs-user_impersonation-on-azure-machine-learning-services) is not an
exception to that. It is not a permission *on your agent*; it is the delegated scope on the
first-party application behind the `https://ai.azure.com` audience, which is what makes Entra willing
to mint the token at all. Authorization to invoke your particular agent still happens afterwards, in
Azure RBAC, against the token that permission let you get.

The full reasoning about the audience is in
[`extension/README.md`](../extension/README.md#entra-app-registration).

---

## 3. Configure, build and package

> **`scripts/setup.sh` step 9 already did this.** If you deployed the agent with it, you answered
> the publisher id and the repository URL at the end of the run, and the script then ran `npm ci`,
> `npm run configure` and `npm run package` and printed the path of the `.vsix` it built — go
> straight to [step 4](#4-install-privately) and upload that file. It skips the build, says so in
> one line and prints these commands instead when either answer was left blank or the machine has
> no `node`/`npm`. Read this section when that happened, when you are rebuilding after a code
> change, or when you want a package configured differently from the deployment (a per-project
> override, an unpinned package, a bumped version).

Nothing tenant-specific is committed in this repository, so packaging has a configuration step in
front of it. `npm run configure` resolves your values and writes them into gitignored files;
`npm run package` refuses to run until it has.

```bash
cd extension
npm ci
npm run configure -- \
  --publisher <your-publisher-id> \
  --repo-url  https://github.com/<your-org>/ticket-to-pr-agent \
  --client-id <entra-application-client-id> \
  --tenant-id <entra-directory-tenant-id>
npm run package
# → dist-vsix/<your-publisher-id>.ticket-to-pr-0.1.1.vsix
```

With an azd environment from `setup.sh` selected, every one of those flags is already in it and
`npm run configure` on its own is enough — [what `configure` reads](#what-configure-reads) is the
full resolution order.

### What `configure` reads

Each value is resolved from the first of these that has it: the **flag**, then the **environment
variable**, then **`azd env get-values`** run in the repository root (skip that with `--no-azd`,
choose a non-current environment with `--azd-env <name>`).

| Value | Flag | Environment variable | From azd |
|---|---|---|---|
| Marketplace publisher id | `--publisher` | `T2PR_EXT_PUBLISHER` | — |
| Repository/Support links | `--repo-url` | `T2PR_EXT_REPO_URL` | — |
| Entra application (client) ID | `--client-id` | `T2PR_ENTRA_CLIENT_ID` | `T2PR_ENTRA_CLIENT_ID` |
| Entra directory (tenant) ID | `--tenant-id` | `T2PR_ENTRA_TENANT_ID` | `T2PR_ENTRA_TENANT_ID`, else `AZURE_TENANT_ID` |
| Agent Application URL | `--agent-url` | `T2PR_AGENT_URL` | `AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` |
| Per-project agent override | `--project <ado-project>=<url>` | — | — |

**The agent URL usually needs no flag.** With an azd environment selected it comes from
`AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` with the trailing `/invocations` and the `?api-version=v1`
query string removed for you — see
[what exactly to paste](#the-agent-application-url--what-exactly-to-paste) for why both have to go.

`--client-id`, `--tenant-id` and `--agent-url` are optional **as a set**: supply all three to pin the
agent for everyone in the organization, or none to ship an unpinned package where each user
configures their own agent in the hub ([step 5](#5-configure-it)). Supplying some but not all is
refused, because the hub silently ignores a half-filled configuration and falls back to per-user
settings — a failure that looks like nothing happening.

`--publisher` is always required.

### What it writes

Three files, all gitignored. `git status` is clean after a full package.

```
extension/deploy.local.json          the resolved values — the source of truth
extension/vss-extension.local.json   the manifest tfx packages, with your publisher id and links
extension/dist/agent-config.json     the settings the installed hub reads at startup
```

Configuration is validated with the extension's **own** validators — `configure.mjs` bundles
`src/host.ts` and calls `validateSettings`, the same function the settings form and the token path
call. A URL, tenant id or scope the page would reject at runtime is rejected here instead, with the
same message, before a VSIX exists. It fails loudly and never packages a placeholder.

To see what is currently configured: `npm run configure -- --print`. To go back to an unconfigured
tree: delete `extension/deploy.local.json`. Full flag list: `npm run configure -- --help`.

### Calling it from a script

`npm run configure` is a thin wrapper. The stable entry point, callable from anywhere including the
repository root, is:

```bash
node extension/scripts/configure.mjs --publisher <id> [--client-id <guid>] [--tenant-id <guid>] \
                                     [--agent-url <url>] [--repo-url <url>] [--project <name>=<url>] \
                                     [--azd-env <name>] [--no-azd] [--version <x.y.z>] [--print]
```

Every flag has the environment variable in the table above as an equivalent, so a script can pass
values either way. It resolves paths relative to the script itself and runs `azd env get-values`
with the repository root as its working directory, so the caller's own directory does not matter.
It requires `npm ci` to have been run in `extension/` first — it borrows the extension's own
esbuild to load and apply `src/host.ts`'s validators — and says so if it has not. Exit status is 0
on success and 1 on any resolution or validation failure, with the reason on stderr.

### What the build produces

`npm run build` type-checks with `tsc --strict`, bundles to `dist/`, and merges `deploy.local.json`
into `dist/agent-config.json` (falling back to the empty tracked template at
`src/agent-config.json` when there is no `deploy.local.json`):

```
dist/hub.html            the page
dist/hub.js              the bundle (page logic + ADO SDK + MSAL)
dist/styles.css          the stylesheet
dist/agent-config.json   administrator-pinned settings (see step 5)
```

Only `dist/` and `images/` are packaged — the manifest says so explicitly. The TypeScript sources,
the dev harness under `extension/dev/`, `deploy.local.json` and `node_modules` are not in the VSIX.

If `tfx` complains about the manifest, the two usual causes are a publisher id you do not own and a
`version` that is not greater than the version already published (see
[step 9](#9-updating-and-uninstalling)).

---

## 4. Install privately

1. Go to <https://marketplace.visualstudio.com/manage>, select your publisher, and choose
   **New extension → Azure DevOps**. Upload the `.vsix` from `dist-vsix/`.
2. **Wait for verification to finish — about 100 seconds.** This is not optional and it is not
   instant. A freshly uploaded version sits in *Verifying* while the Marketplace scans it, and until
   it clears, the extension does not exist as far as anything else is concerned:

   - its Marketplace page returns **404**, and
   - the extension management API returns **`ExtensionDoesNotExistException`**.

   Neither error says "still verifying", so it reads exactly like a failed upload and invites you to
   re-upload — which restarts the clock. Refresh the publisher management page until the version's
   flags read **`validated`**, then continue.
3. Leave it **private**. `"public": false` in the manifest already sets this; the Marketplace shows
   the extension as *Private* and it is not listed publicly or searchable.
4. **Share** it with the organizations that may install it: on the extension's row, **… → Share/
   Unshare**, then add your organization name (the `dev.azure.com/<org>` segment).
5. In the organization: **Organization settings → Extensions → Shared** tab → select **Ticket to
   PR** → **Get it free / Install**. This needs the **Manage extensions** permission.
6. The hub appears under **Repos → Ticket to PR** in every project of that organization.

To publish from the command line instead of the web UI:

```bash
npx tfx extension publish \
  --manifest-globs vss-extension.local.json \
  --share-with <your-org> \
  --token <marketplace-pat>
```

Note `vss-extension.local.json` — the generated manifest from [step 3](#3-configure-build-and-package),
not the tracked placeholder one. The same ~100 second verification wait applies before the shared
organization can install it.

That personal access token needs the **Marketplace (manage)** scope and is used from your own
machine to talk to the Marketplace. It is not part of the package and never reaches the
organization — the extension itself contains no PAT, API key or client secret.

---

## 5. Configure it

Three values decide which agent the page talks to and how it signs in, plus one constant:

| Field in the hub | Example | What the page enforces |
|---|---|---|
| **Agent Application URL** | `https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols` | `https`, host ending in `.services.ai.azure.com`, no user info, no query string, no fragment |
| **Entra application (client) ID** | the SPA registration from [step 2](#2-register-the-entra-application) | must be a GUID |
| **Entra directory (tenant) ID** | your directory | must be a GUID — see [2.2](#22-it-must-be-multi-tenant-even-for-a-single-tenant-organization) for what it does and does not do |
| **Agent scope** | `https://ai.azure.com/.default` | must be exactly that one string |

A value that fails these rules is refused by the settings form **and** discarded when read back
from storage — before any token is requested. There is no configuration path that gets a token
sent to a host that has not been through the check.

### The Agent Application URL — what exactly to paste

**Paste the agent's invocations endpoint with the trailing `/invocations` and any query string
removed.** The page appends `/invocations` and `/invocations/{runId}` itself, so leaving them on
produces `…/invocations/invocations` and the request fails.

What `azd` gives you:

```bash
azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT
# https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols/invocations?api-version=v1
```

What goes in the settings box:

```
https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols
```

Strip it mechanically if you prefer — or let `npm run configure` do exactly this for you
([step 3](#3-configure-build-and-package)):

```bash
azd env get-value AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT | sed -e 's/?.*$//' -e 's#/invocations$##'
```

Both the query string and the `/invocations` segment have to go. The form refuses a URL carrying
either rather than silently trimming it, so the mistake surfaces in the settings panel instead of as
a 404 in the middle of a run.

The shape of a Foundry agent endpoint, for reference
([contract.md §1](contract.md#1-endpoints)):

```
https://{account}.services.ai.azure.com/api/projects/{project}/agents/{agent}/endpoint/protocols[/invocations]
                                                                                                └── the page adds this
```

### `api-version` is required on every call

**Every call to the Foundry gateway must carry `?api-version=v1`** — the POST and the polling GET
alike. Without it the gateway rejects the request before the agent container is ever reached:

```
400 BadRequest: Missing required query parameter: api-version
```

There is nothing to configure. `agentEndpoint()` in `src/host.ts` appends it to every request the
page makes, which is precisely why the configured base URL must not already carry a query string —
two sources of query parameters on one URL is how you get a value that works until it doesn't.

If you are reproducing a call with `curl`, add it there too; the example in
[deploy.md §6](deploy.md#6-verify) does.

### The audience

The **Agent scope** is `https://ai.azure.com/.default`, always. It is not a per-tenant or
per-agent value, there is nothing to look up, and the page accepts no other string — the check is
an exact, case-sensitive comparison against a one-entry list, not a pattern.

That strictness is the point. `https://ai.azure.com` is a **shared, first-party audience**: the
token minted for it is the signed-in user's whole Foundry data-plane access, not a credential
private to this agent. A pattern loose enough to admit it would equally admit
`https://graph.microsoft.com/.default` or `https://management.azure.com/.default`, and the page
would fetch a token for whichever the settings named and post it to the configured host. Same
reason the host is pinned: the token can only ever reach a Microsoft-operated Foundry gateway.

The application behind that audience is **Azure Machine Learning Services**
(`18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`), and your registration needs a consented
`user_impersonation` grant on it or no token is issued at all —
[2.3](#23-it-needs-user_impersonation-on-azure-machine-learning-services) and
[2.4](#24-it-needs-a-service-principal-and-a-consent-grant).

An earlier revision of this project used an `api://…` audience on the theory that `azd up`
registered an application for the agent exposing a delegated scope. **That was wrong** — no such
registration exists, and the extension as it then stood could not have obtained a token the agent
would accept. The `api://` form is gone; if you find it in notes or a fork, it is stale.

### Option A: pin it in the package

Pass the values to `npm run configure` ([step 3](#3-configure-build-and-package)) before packaging.
It writes them to the untracked `extension/deploy.local.json`, and `npm run build` merges them into
`dist/agent-config.json`, which the hub reads at startup:

```jsonc
// extension/dist/agent-config.json — generated, gitignored, never hand-edited
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

`default` applies everywhere in the organization; a `projects` entry overrides it for that project,
matched on **project name**, case-insensitively. Add one with
`--project "Payments=https://…/endpoint/protocols"`, repeatable.

The tracked template at `extension/src/agent-config.json` is deliberately **empty** and stays that
way — it is a public repository, and one tenant's agent URL and app registration are not a default
anybody else wants. Editing it by hand is how a fork ends up shipping somebody else's identifiers.

When the packaged file yields a complete, valid set of values the hub uses them for **everyone in
the organization** and shows the settings form read-only, with a note saying so. A half-filled set is
ignored rather than half-used, which is why `configure` refuses to write one.

Changing these values means re-running `configure`, repackaging, and updating the installed
extension — which requires the organization's **Manage extensions** permission. That permission *is*
the access control on these values.

### Option B: per-user settings

If you package without pinned settings — no `--client-id`, `--tenant-id` or `--agent-url` — each
user configures the extension for themselves: open the hub, expand **Agent settings**, fill in the
fields, **Save settings**. Until they are all set, a banner stays up and **Create PR with agent** is
disabled.

The document is written at the extension data service's **User** scope, so it is readable and
writable only by its owner. One user cannot change what another user's page does.

### Which one to use

**Use Option A in any real organization.** Everyone gets a working hub on first open, nobody has to
be told a URL, and the values can only be changed by someone who can already install extensions.

Use Option B when there is no single answer to pin — a fork where people point at their own
development agents, a trial, or an organization whose projects each have a different Foundry
project and you would rather not maintain a `projects` block.

What you cannot have is a project-scoped, access-controlled settings store: Azure DevOps offers
extension data at exactly two scopes, organization-wide (`Default`) and per-user (`User`), with no
per-document ACL. Every user of the hub holds `vso.extension.data_write`, so an organization-wide
document would be writable by any member of the organization — and since these settings name the
host a token is sent to, that would be a token-redirection primitive rather than a preference. The
extension therefore never reads or writes the organization-wide scope. The full argument, including
the residual exposure and why it is bounded, is under **Known limitations** in
[`extension/README.md`](../extension/README.md#known-limitations).

---

## 6. Using it

**Where:** any project in the organization → **Repos** → **Ticket to PR** in the left-hand hub
list.

**The flow:**

1. **Pick a work item.** The list shows items assigned to you and items you recently viewed in this
   project. If the one you want is not there, type its ID in **Search by ID or title** and press
   Enter to fetch it directly.
2. **Pick a repository** from the project's repositories.
3. **Base branch** (optional). Leave it empty to use the repository's default branch. It is free
   text and is not checked against the repository's actual branches — a wrong value surfaces later
   as `clone_failed`.
4. **Branch name.** Prefilled as `{type-slug}/{id}-{title-slug}`, e.g. `bugfix/4123-null-ref`. Edit
   it freely; it must match `^[A-Za-z0-9._/-]+$`, and the page also rejects refs git itself would
   refuse (leading or trailing `/`, `//`, `..`, a trailing `.lock`).
5. **Create PR with agent.** The page acquires a token — the first time, this may raise a sign-in
   popup for consent — and POSTs the contract body.

**What you see next.** The status panel shows six steps in order:

```
Queued → Reading the work item → Cloning the repository → Writing the code
      → Pushing the branch → Creating the pull request → Done
```

The page polls `GET /invocations/{runId}` **every 5 seconds** and updates the current phase. Each
change is announced to screen readers through an `aria-live` region. Coding runs take minutes;
**Writing the code** is where most of the time goes.

**Polling behaviour worth knowing:**

- A permanent failure — 401, 403, 404, or a response body that does not match the contract — stops
  the loop after a single request and puts the reason on screen. It does not retry into a wall.
- A transport failure, a 5xx, a 408 or a 429 is retried, **five times in a row at most**. After
  that the page says it lost contact and suggests checking the repository — the run itself may
  still be going on the server.
- Token acquisition on the polling path is **silent-only**. If a silent token cannot be obtained
  mid-run, polling stops and says so rather than raising a sign-in popup every five seconds.
- Polling gives up after 45 minutes.
- Submitting again cancels the previous run's poll chain, so the request rate cannot compound.
- **Closing the tab stops the polling, not the run.** The agent keeps working; you can find the
  pull request in the repository when it lands. There is no way to re-attach the page to a run id.

**On success** the panel links to the pull request. The agent raises the PR and stops — it never
merges. On failure it shows the failure as a sentence; all eleven contract error codes are rendered
as English, with the raw code shown as secondary detail so you can quote it in a support request.

---

## 7. Troubleshooting

Sign-in and token errors first — these are the ones that cost people afternoons, and every one of
them has been seen and fixed on a live installation.

| Symptom | What it means | What to do |
|---|---|---|
| **`AADSTS50194` — "not configured as a multi-tenant application … /common endpoint is not supported"** | The app registration is single-tenant. Inside Azure DevOps the host frame brokers the token on `/common` and ignores the authority your MSAL client was configured with, so setting the tenant id does not help. | Make the registration multi-tenant: `az ad app update --id <client-id> --sign-in-audience AzureADMultipleOrgs`. Full explanation: [2.2](#22-it-must-be-multi-tenant-even-for-a-single-tenant-organization). Keep the tenant id configured as well — it is still what the non-brokered paths use. |
| **`AADSTS650057: Invalid resource`** | The registration does not declare `user_impersonation` on the application behind the `https://ai.azure.com` audience. | Add delegated `user_impersonation` (`1a7925b5-f871-417a-9b8b-303f9f29fa10`) on resource app `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`, **Azure Machine Learning Services** — searchable only by that GUID. [2.3](#23-it-needs-user_impersonation-on-azure-machine-learning-services). |
| **`AADSTS650057: Invalid resource` … `List of valid resources from app registration: (empty)`** | Different cause, same code. The permission is declared but never consented, and the page asks for `.default`, which returns only *already consented* permissions. `(empty)` is the giveaway. | Create the service principal and the consent grant: `az ad sp create --id <client-id>` then `az ad app permission admin-consent --id <client-id>`. Verify an `oauth2PermissionGrant` exists — [2.4](#24-it-needs-a-service-principal-and-a-consent-grant). |
| **`400 BadRequest: Missing required query parameter: api-version`** | A gateway call went out without `?api-version=v1`. The page always adds it, so this means the configured URL is being used somewhere that bypasses `agentEndpoint()` — or you are reproducing the call by hand. | Add `?api-version=v1` to your `curl`. If the page itself produces it, the configured base URL almost certainly carries its own query string; re-read [what exactly to paste](#the-agent-application-url--what-exactly-to-paste). |
| **404 on the Marketplace page, or `ExtensionDoesNotExistException` from the API, right after upload** | Marketplace verification has not finished. It takes roughly **100 seconds** and the extension genuinely does not exist until it does. | Wait, refresh the publisher management page until the version's flags read `validated`, then install. Do not re-upload — that restarts the clock. [Step 4](#4-install-privately). |
| **A consent prompt appears on first use, or sign-in fails with a consent error** | The tenant requires admin consent and the grant is missing or is per-user. | Admin-consent once for the registration ([2.4](#24-it-needs-a-service-principal-and-a-consent-grant)); the grant then reads `consentType: AllPrincipals` and nobody is prompted again. |

Then everything else:

| Symptom | What it means | What to do |
|---|---|---|
| **The banner says the project is not connected to an agent** | No complete, valid settings — the packaged `agent-config.json` is empty and this user has saved nothing. | Configure it: [step 5](#5-configure-it). If you pinned values in the package but the banner is still up, the pinned set is incomplete or one value failed validation; check the URL has no trailing `/invocations` and no query string, and that both GUIDs are GUIDs. `npm run configure -- --print` shows what was packaged. |
| **The settings form is read-only** | Values are pinned in the installed package. Working as intended. | To change them, re-run `npm run configure` with the new values, bump the version, repackage and update the installed extension ([step 9](#9-updating-and-uninstalling)). |
| **`npm run package` refuses to run** | There is no `extension/deploy.local.json`, so there is nothing to package but placeholders. | `npm run configure -- --publisher <your-publisher-id>` ([step 3](#3-configure-build-and-package)). This is deliberate: a package built from the tracked placeholders cannot be uploaded anywhere. |
| **The URL is refused by the form** | It failed one of the rules: not `https`, host not ending in `.services.ai.azure.com`, or it carries user info, a query string or a fragment. | Paste the Foundry endpoint with `/invocations` and any `?api-version=…` stripped — see [what exactly to paste](#the-agent-application-url--what-exactly-to-paste). A sovereign-cloud endpoint cannot be accepted by this build at all; it needs the constants changed and a rebuild. |
| **The scope is refused by the form** | It is not exactly `https://ai.azure.com/.default`. | Type that string exactly. Case matters; no trailing slash; no second scope after a space. The `api://…` form some older notes mention is wrong and is not accepted. |
| **401 from the agent** | The token's **audience** is not what the agent's gateway expects. | Confirm the Agent scope is `https://ai.azure.com/.default` — not the ARM audience, not the Azure DevOps one. Then confirm the Entra client ID is the SPA registration from [step 2](#2-register-the-entra-application) and that its redirect URI is `https://dev.azure.com/_public/_MsalPopup`. If curl with `az account get-access-token --resource https://ai.azure.com` also gets a 401 ([deploy.md §6](deploy.md#6-verify)), the problem is the agent deployment, not the extension. |
| **403 from the agent** | The token is fine; **the signed-in user has no data-plane role on the agent.** | Assign **Foundry Agent Consumer** on the Foundry project or the agent — [deploy.md §5](deploy.md#5-let-callers-invoke-the-agent). Note that `Owner` and `Contributor` on the subscription do **not** grant it. Assign it to an Entra group and add people there. |
| **404, or the run never starts** | Usually the URL shape. | The most common cause is a double `/invocations` from leaving the segment on the configured URL. Check the value against [what exactly to paste](#the-agent-application-url--what-exactly-to-paste). |
| **"Lost contact with the agent"** | Five consecutive transport failures or 5xx responses. | The run may still be going — check the repository for a new pull request before resubmitting. Then check the agent's own health: `azd ai agent monitor` ([deploy.md §8](deploy.md#8-troubleshooting)). |
| **The work item list is empty** | Nothing assigned to you and nothing recently viewed **in this project**. | Search by ID. Recent activity is an organization-wide feed filtered to the current project, so items in other projects will not appear. |
| **`branch_exists`** | The branch is already on the remote. | Change the branch name and submit again. The page cannot detect this client-side; only the server knows. |
| **`ado_forbidden`, `work_item_not_found`, `repository_not_found`, `clone_failed`, `push_failed`, `pr_failed`** | These come from the **agent**, not the extension. They are almost always the agent identity's Azure DevOps permissions or the organization allowlist. | [deploy.md §8](deploy.md#8-troubleshooting) has a row for each; the codes themselves are defined in [contract.md §6](contract.md#6-error-codes). |
| **The hub page is blank or does not load** | Extension not installed in this organization, or the browser blocked the framed page. | Confirm **Organization settings → Extensions → Installed** lists Ticket to PR. Then check the browser console for a blocked resource — the hub renders in a cross-origin iframe inside `dev.azure.com`. |

Two things this page will never show you: a bearer token, and any work-item text rendered as HTML.
Tokens are acquired and attached to requests below the UI layer and are not reachable by the page
logic; all attacker-influenceable text — work item titles, repository names, agent error messages —
goes into the DOM as text nodes only. See
[`extension/README.md`](../extension/README.md#security-notes).

---

## 8. Known unverified

What is on this list has shrunk a lot since the extension was first written: it has now been
packaged, installed and used in a live Azure DevOps organization against a deployed agent. Work item
listing, repository listing, search by id, branch prefill, token acquisition, submission, the polling
loop and a **complete run through to a pull request** were all exercised there. The Entra facts in
[step 2](#2-register-the-entra-application) and the `api-version` requirement in
[step 5](#api-version-is-required-on-every-call) came out of that, and are stated as
facts rather than listed here.

What is still genuinely open:

1. **Per-user consent has not been exercised.** The installation this was verified against uses
   tenant-wide admin consent (`consentType: AllPrincipals`,
   [2.4](#24-it-needs-a-service-principal-and-a-consent-grant)). Whether a tenant that permits user
   consent produces the grant cleanly at each user's first sign-in inside the Azure DevOps popup —
   rather than failing in the brokered NAA flow, which cannot show an arbitrary consent screen — has
   not been tested. If you rely on it and it fails, admin consent once is the fix.
3. **Sovereign clouds are not supported by this build and have not been tried.** The agent host
   suffix `.services.ai.azure.com` and the audience `https://ai.azure.com/.default` are compiled-in
   constants in `extension/src/host.ts`. A sovereign cloud uses different values for both, so it
   needs a reviewed code change and a rebuild — deliberately, rather than a text box that could send
   a first-party token anywhere.
4. **Azure DevOps Server (on-premises) is untested.** The manifest targets
   `Microsoft.VisualStudio.Services`, which covers Server 2019+, but nested app authentication and
   the `https://dev.azure.com/_public/_MsalPopup` redirect are hosted-service behaviours. Nothing
   here has been run against an on-premises collection.

The developer-facing list, including what *was* verified against current documentation and how, is
at the end of [`extension/README.md`](../extension/README.md#what-was-verified).

---

## 9. Updating and uninstalling

**Update.** Bump `version` in the tracked `extension/vss-extension.json` — the Marketplace requires
each upload to be strictly greater than the last — then:

```bash
cd extension
npm run package        # regenerates vss-extension.local.json from the tracked manifest
```

and upload the new VSIX over the existing extension (**… → Update** on the Marketplace management
page). The ~100 second verification wait from [step 4](#4-install-privately) applies to every
upload, not just the first. Organizations that have it installed pick the new version up
automatically once it is validated; there is nothing to do per organization.

`npm run configure -- --version <x.y.z>` overrides the version in the generated manifest without
touching the tracked one. That is for iterating locally — a version you intend to keep belongs in
`vss-extension.json`, committed.

Changed settings ship the same way. Re-run `npm run configure` with the new values, bump the
version, repackage, upload. There is no way to change packaged settings without a new version, and
that is the point: the **Manage extensions** permission is the access control on them.

**Uninstall.** **Organization settings → Extensions → Installed → Ticket to PR → … → Uninstall**.
The hub disappears from Repos immediately.

- Per-user settings documents are extension data and go with the extension.
- **Nothing about the agent changes.** The agent is a separate deployment; uninstalling the
  extension only removes this UI. Runs already in flight continue and their pull requests still
  appear. Tear the agent down separately with `azd down` ([deploy.md §9](deploy.md#9-tearing-it-down)).
- The Entra app registration from [step 2](#2-register-the-entra-application) is not removed by
  uninstalling, and neither is its `oauth2PermissionGrant`. Delete both in the portal, or
  `az ad app delete --id <client-id>`, if nothing else uses them.
- **Unshare** the extension from the organization on the Marketplace page if you want to prevent it
  being reinstalled, and **remove the Foundry Agent Consumer** assignments if the users should no
  longer be able to invoke the agent by any means — uninstalling the UI does not revoke access to
  the agent.

---

## Related

| | |
|---|---|
| [`extension/README.md`](../extension/README.md) | Developer-facing: project layout, the dev harness, headless checks, and the reasoning behind each validation rule. |
| [`docs/contract.md`](contract.md) | The request/response interface the page speaks, and the eleven error codes. |
| [`docs/deploy.md`](deploy.md) | Deploying the agent, the Azure DevOps identity and permissions, and the RBAC the hub's users need. |
| [`docs/configuration.md`](configuration.md) | Every agent environment variable. |
| [`docs/architecture.md`](architecture.md) | Where the extension sits in the system, and the trust boundaries. |
| [`SECURITY.md`](../SECURITY.md) | Reporting process and the project's security posture. |
