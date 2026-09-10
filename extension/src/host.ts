import type { CreateRunRequest, CreateRunResponse, RunStatus } from "./contract.js";

/**
 * The seam between the UI (hub.ts) and everything outside the page.
 *
 * hub.ts talks only to this interface, so the Azure DevOps extension SDK, MSAL
 * and `fetch` are all substitutable. `ado-host.ts` is the real implementation;
 * `dev/stub-host.ts` is the local development stub. The UI code is identical in
 * both cases — the dev harness is not a copy of the hub.
 *
 * Note what is deliberately absent: there is no `getToken()`. Bearer tokens are
 * acquired and used entirely inside the host implementation, so the UI layer
 * cannot log, render or otherwise leak one.
 */
export interface HubHost {
  /** Resolve page context. Called once, before anything is rendered. */
  initialize(): Promise<HubContext>;

  /** Tell the host frame the extension has rendered. No-op in the dev stub. */
  notifyReady(): void;

  /**
   * Settings for this project. Either pinned by the packaged extension (managed)
   * or stored per user; see `ado-host.ts` and README "Configure the extension".
   */
  loadSettings(): Promise<LoadedSettings>;
  saveSettings(settings: HubSettings): Promise<void>;

  /** Work items assigned to, or recently touched by, the signed-in user. */
  listMyWorkItems(): Promise<WorkItemSummary[]>;
  /** Look up a single work item by id. Resolves undefined when not found. */
  findWorkItemById(id: number): Promise<WorkItemSummary | undefined>;

  /** Git repositories in the current project. */
  listRepositories(): Promise<RepositorySummary[]>;

  /** POST /invocations. Acquires and attaches the bearer token internally. */
  submitRun(request: CreateRunRequest): Promise<CreateRunResponse>;
  /** GET /invocations/{runId}. */
  getRunStatus(runId: string): Promise<RunStatus>;

  /** Navigate the top-level window (the hub runs in an iframe). */
  openUrl(url: string): void;
}

export interface HubContext {
  /** ADO organization name, not URL. Sent as `organization` in the contract. */
  organization: string;
  /** ADO project name. Sent as `project` in the contract. */
  project: string;
  /** Display name of the signed-in user, for the settings-permission hint. */
  userDisplayName: string;
}

export interface HubSettings {
  /** Base URL of the deployed Agent Application, e.g. https://my-agent.azurewebsites.net */
  agentUrl: string;
  /** Entra client (application) id of the SPA registration this page signs in as. */
  entraClientId: string;
  /**
   * Entra directory (tenant) id the registration lives in.
   *
   * Without it MSAL signs in through the shared `/common` authority, which a
   * single-tenant application refuses: `AADSTS50194 ... not configured as a multi-tenant
   * application. Usage of the /common endpoint is not supported`. Verified against a live
   * deployment.
   */
  entraTenantId: string;
  /**
   * Scope requested for the Agent Application. The audience of the bearer token
   * sent to the agent, and the only value `validateAgentScope` accepts:
   * `https://ai.azure.com/.default`, the Microsoft Foundry data plane.
   */
  agentScope: string;
}

export const EMPTY_SETTINGS: HubSettings = {
  agentUrl: "",
  entraClientId: "",
  entraTenantId: "",
  agentScope: "",
};

export interface LoadedSettings {
  settings: HubSettings;
  /**
   * True when the values came from the packaged extension rather than from the
   * per-user settings document, i.e. an administrator pinned them by packaging
   * and installing the extension. The form is read-only in that case.
   */
  managed: boolean;
}

/**
 * An error from the agent, or from the configuration that decides which agent to
 * talk to. `status` is the HTTP status when there was a response at all.
 *
 * `retryable` is what the poll loop keys off. A poll that fails must not be
 * retried for 45 minutes when the failure is permanent — a 403 or a 404 for an
 * unknown run id will still be a 403 or a 404 on the 540th request. By default
 * only a transport failure (no status) or a 5xx/408/429 is retryable; anything
 * a caller knows to be terminal (bad configuration, sign-in required, a body the
 * extension cannot parse) passes `retryable: false` explicitly.
 */
export class AgentRequestError extends Error {
  readonly status?: number;
  readonly retryable: boolean;

  constructor(message: string, options: { status?: number; retryable?: boolean } = {}) {
    super(message);
    this.name = "AgentRequestError";
    this.status = options.status;
    this.retryable = options.retryable ?? isRetryableStatus(options.status);
  }
}

/** True for failures where the same request could plausibly succeed later. */
export function isRetryableStatus(status: number | undefined): boolean {
  if (status === undefined) return true; // transport error: no response at all
  if (status === 408 || status === 429) return true;
  return status >= 500;
}

/** True when an error should be retried by the poll loop rather than reported. */
export function isRetryableError(error: unknown): boolean {
  if (error instanceof AgentRequestError) return error.retryable;
  // A host that is not `ado-host.ts` (or an unexpected throw) gets the benefit of
  // the doubt, bounded by the consecutive-failure cap in hub.ts.
  return true;
}

export interface WorkItemSummary {
  id: number;
  /** ADO work item type name, e.g. "Bug". Free string per the contract. */
  type: string;
  title: string;
  state: string;
  /** Why this item is in the list. Shown as a grouping hint. */
  source: "assigned" | "recent";
}

export interface RepositorySummary {
  id: string;
  name: string;
  /** Full ref, e.g. "refs/heads/main". Undefined for an empty repository. */
  defaultBranch?: string;
  isDisabled?: boolean;
}

/** Short branch name from a full ref, for display and for the baseBranch placeholder. */
export function shortBranchName(ref: string | undefined): string | undefined {
  if (!ref) return undefined;
  return ref.startsWith("refs/heads/") ? ref.slice("refs/heads/".length) : ref;
}

export interface Validation {
  valid: boolean;
  /** A sentence explaining the problem, or undefined when valid. */
  message?: string;
}

/** True once every setting the page needs to submit a run is present and valid. */
export function isConfigured(settings: HubSettings): boolean {
  return validateSettings(settings).valid;
}

/**
 * Hosts that may receive the token. A Microsoft Foundry project endpoint is
 * `https://{account}.services.ai.azure.com/api/projects/{project}`, and a hosted
 * agent hangs off it, so every legitimate agent URL sits under this suffix.
 */
const AGENT_HOST_SUFFIX = ".services.ai.azure.com";

/**
 * The page sends an Entra bearer token to this origin, so it must be a real TLS
 * origin, it must be unambiguous, and — since `validateAgentScope` now pins the
 * audience to `https://ai.azure.com/.default` — it must be a Foundry host.
 *
 * That last rule carries real weight. `https://ai.azure.com` is a *shared
 * first-party audience*: the token minted for it is the signed-in user's
 * Foundry data-plane token, accepted by every Foundry project and agent that
 * user can reach, not just this one. An unrestricted host here would turn the
 * settings field into a credential-exfiltration channel — point it anywhere and
 * that token is posted there. Requiring `*${AGENT_HOST_SUFFIX}` means the token
 * can only ever be sent to a Microsoft-operated Foundry gateway, which does not
 * forward the `Authorization` header to any container behind it. So even a URL
 * naming somebody else's Foundry account leaks nothing to their code.
 *
 * `https://agent.example@evil.com/` parses as host `evil.com` with the userinfo
 * `agent.example`, which reads to a human as the trusted host and resolves to
 * the attacker's. Userinfo, query strings and fragments are all rejected: none
 * of them belong in a base URL that only ever gets `/invocations` appended, and
 * each is a way to make a hostile URL look benign in the settings field.
 */
export function validateAgentUrl(value: string): Validation {
  const raw = value.trim();
  if (!raw) return { valid: false, message: "Enter the Agent Application URL." };
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { valid: false, message: "That is not a valid URL. Include the scheme, for example https://my-agent.example.com." };
  }
  if (url.protocol !== "https:") {
    return {
      valid: false,
      message: "The Agent Application URL must use https. The page sends an access token to it.",
    };
  }
  if (url.username || url.password) {
    return {
      valid: false,
      message:
        "The Agent Application URL must not contain a username or password. A URL like https://agent.example@evil.com/ points at evil.com, not at agent.example.",
    };
  }
  if (url.search || url.hash) {
    return {
      valid: false,
      message:
        "The Agent Application URL must not contain a query string or a fragment. Enter the base URL only, for example https://my-agent.example.com.",
    };
  }
  if (!url.hostname) {
    return { valid: false, message: "The Agent Application URL has no host name." };
  }
  // `hostname` is already lower-cased and punycoded by the URL parser, so a
  // homograph host cannot match this suffix by looking like it.
  if (
    !url.hostname.endsWith(AGENT_HOST_SUFFIX) ||
    url.hostname.length === AGENT_HOST_SUFFIX.length
  ) {
    return {
      valid: false,
      message:
        "The Agent Application URL must be a Microsoft Foundry project endpoint, so its host has to end in .services.ai.azure.com — for example https://my-account.services.ai.azure.com/api/projects/my-project/agents/ticket-to-pr/endpoint/protocols. The page sends your Foundry access token to this host, and that token works against every Foundry project you can reach, so it is not sent anywhere else.",
    };
  }
  return { valid: true };
}

/**
 * The audiences this page will ask Entra for. An exact-literal allowlist, and
 * deliberately not a pattern.
 *
 * The settings document decides which audience the page requests a token for,
 * so anything looser lets whoever writes that document name a different
 * resource — `https://graph.microsoft.com/.default`,
 * `https://management.azure.com/.default`, `https://vault.azure.net/.default`,
 * the Azure DevOps resource GUID — and collect a token for it. Note that a
 * pattern shaped like `https://…/.default` would admit every one of those; a
 * list of literals cannot be widened by accident.
 *
 * The single entry is the Microsoft Foundry data-plane audience. This agent is
 * a Foundry hosted agent, invoked through the project's agent endpoint, and
 * Foundry authenticates that call with a token for `https://ai.azure.com`:
 * "Microsoft Entra ID uses OAuth 2.0 bearer tokens scoped to
 * https://ai.azure.com/.default" (Authentication and authorization in Microsoft
 * Foundry), and the Invocations sample calls
 * `credential.get_token("https://ai.azure.com/.default")` before posting to
 * `{project_endpoint}/agents/{name}/endpoint/protocols/invocations` (Deploy a
 * hosted agent).
 *
 * There is deliberately no `api://…/.default` entry. A hosted agent does get its
 * own Entra agent identity, but that identity is for the agent's *outbound*
 * calls; it exposes no delegated scope for inbound callers, and who may invoke
 * the agent is decided by Azure RBAC on the Foundry project or agent, not by an
 * app registration of the agent's own. No deployment this repository documents
 * produces an `api://` audience, so accepting one could only ever send a token
 * somewhere the agent cannot use it.
 */
export const FOUNDRY_AGENT_SCOPE = "https://ai.azure.com/.default";

/** The whole allowlist. One entry today; `validateAgentScope` compares against it exactly. */
export const ALLOWED_AGENT_SCOPES: readonly string[] = [FOUNDRY_AGENT_SCOPE];

export function validateAgentScope(value: string): Validation {
  const raw = value.trim();
  if (!raw) {
    return {
      valid: false,
      message: `Enter the scope for the Agent Application. It is always ${FOUNDRY_AGENT_SCOPE}.`,
    };
  }
  // Exact match, case-sensitive: ".DEFAULT" is not the same scope string, and a
  // value carrying a second scope after a space or a newline is not this one.
  if (!ALLOWED_AGENT_SCOPES.includes(raw)) {
    return {
      valid: false,
      message: `The agent scope must be exactly ${FOUNDRY_AGENT_SCOPE} — the Microsoft Foundry data plane, which is what a hosted agent's endpoint authenticates. Scopes for any other resource are refused, because this page hands the resulting token to the Agent Application URL.`,
    };
  }
  return { valid: true };
}

const GUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/**
 * The directory the registration lives in.
 *
 * A GUID, not `common` or `organizations`: MSAL defaults to the shared `/common`
 * authority when none is given, and a single-tenant application refuses it with
 * `AADSTS50194`. Naming the tenant is also the narrower choice — sign-in is confined to
 * one directory rather than any Entra tenant that happens to hold a matching app id.
 */
export function validateEntraTenantId(value: string): Validation {
  const raw = value.trim();
  if (!raw) {
    return {
      valid: false,
      message:
        "Enter the Entra directory (tenant) ID. A single-tenant application cannot sign in through the shared /common endpoint.",
    };
  }
  if (!GUID_PATTERN.test(raw)) {
    return {
      valid: false,
      message:
        "The Entra directory (tenant) ID is a GUID, for example 00000000-0000-0000-0000-000000000000.",
    };
  }
  return { valid: true };
}

/** The authority MSAL signs in against: this directory, never `/common`. */
export function entraAuthority(tenantId: string): string {
  return `https://login.microsoftonline.com/${tenantId.trim()}`;
}

export function validateEntraClientId(value: string): Validation {
  const raw = value.trim();
  if (!raw) {
    return { valid: false, message: "Enter the Entra application (client) ID for this extension." };
  }
  if (!GUID_PATTERN.test(raw)) {
    return {
      valid: false,
      message:
        "The Entra application (client) ID is a GUID, for example 00000000-0000-0000-0000-000000000000.",
    };
  }
  return { valid: true };
}

/**
 * Validates a whole settings document. Called on the way in (stored values are
 * not trusted), on the way out (the settings form) and again immediately before
 * a token is requested, so no code path can send a bearer token to a URL that
 * has not been through this function.
 */
export function validateSettings(settings: HubSettings): Validation {
  const url = validateAgentUrl(settings.agentUrl);
  if (!url.valid) return url;
  const clientId = validateEntraClientId(settings.entraClientId);
  if (!clientId.valid) return clientId;
  const tenantId = validateEntraTenantId(settings.entraTenantId);
  if (!tenantId.valid) return tenantId;
  return validateAgentScope(settings.agentScope);
}

/** True for an absolute https URL with no credentials in it. */
export function isSafeHttpsUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password;
  } catch {
    return false;
  }
}

/** Joins the configured agent base URL with a contract path. */
/** The Foundry gateway's API version. Required on every call. */
export const AGENT_API_VERSION = "v1";

/**
 * Build a call URL for the agent endpoint.
 *
 * `api-version` is not optional. Without it the Foundry gateway answers every request —
 * POST and GET alike — with `400 BadRequest: Missing required query parameter:
 * api-version`, before the agent container is ever reached. Verified against a live
 * deployment.
 */
export function agentEndpoint(agentUrl: string, path: string): string {
  const base = `${agentUrl.replace(/\/+$/, "")}${path}`;
  return `${base}${base.includes("?") ? "&" : "?"}api-version=${AGENT_API_VERSION}`;
}
