import * as SDK from "azure-devops-extension-sdk";
import {
  createNestablePublicClientApplication,
  InteractionRequiredAuthError,
  type IPublicClientApplication,
  type AccountInfo,
} from "@azure/msal-browser";

import {
  isCreateRunResponse,
  isRunStatus,
  type CreateRunRequest,
  type CreateRunResponse,
  type RunStatus,
} from "./contract.js";
import {
  AgentRequestError,
  EMPTY_SETTINGS,
  agentEndpoint,
  entraAuthority,
  validateSettings,
  type HubContext,
  type HubHost,
  type HubSettings,
  type LoadedSettings,
  type RepositorySummary,
  type WorkItemSummary,
} from "./host.js";

// Re-exported for callers that used to import it from here.
export { AgentRequestError } from "./host.js";

/*
 * Verified against the installed packages and current docs (see extension/README.md):
 *  - azure-devops-extension-sdk 5.0.0: init/applyTheme/getHost/getUser/getService/
 *    getAccessToken/enableNestedAppAuth all present in SDK.d.ts.
 *  - @azure/msal-browser 5.21.0: createNestablePublicClientApplication present.
 *  - Azure DevOps REST 7.1: wiql, workitems batch, git repositories,
 *    work/accountmyworkrecentactivity.
 */

const API_VERSION = "7.1";
const EXTENSION_DATA_SERVICE = "ms.vss-features.extension-data-service";
const PROJECT_PAGE_SERVICE = "ms.vss-tfs-web.tfs-page-data-service";
const HOST_NAVIGATION_SERVICE = "ms.vss-features.host-navigation-service";

const WORK_ITEM_FIELDS = ["System.Id", "System.WorkItemType", "System.Title", "System.State"];
const MAX_ASSIGNED = 50;
const MAX_RECENT = 25;

interface IDocumentOptions {
  scopeType?: string;
  scopeValue?: string;
  defaultValue?: unknown;
}
interface IExtensionDataManager {
  getValue<T>(key: string, options?: IDocumentOptions): Promise<T>;
  setValue<T>(key: string, value: T, options?: IDocumentOptions): Promise<T>;
}
interface IExtensionDataService {
  getExtensionDataManager(extensionId: string, accessToken: string): Promise<IExtensionDataManager>;
}
interface IProjectInfo {
  id: string;
  name: string;
}
interface IProjectPageService {
  getProject(): Promise<IProjectInfo | undefined>;
}
interface IHostNavigationService {
  openNewWindow(url: string, features: string): void;
}

interface AdoListResponse<T> {
  count: number;
  value: T[];
}

interface WiqlResponse {
  workItems?: { id: number }[];
}

interface AdoWorkItem {
  id: number;
  fields?: Record<string, unknown>;
}

interface AdoRecentActivity {
  id: number;
  workItemType?: string;
  title?: string;
  state?: string;
  teamProject?: string;
  activityDate?: string;
}

interface AdoRepository {
  id: string;
  name: string;
  defaultBranch?: string;
  isDisabled?: boolean;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function toWorkItem(item: AdoWorkItem, source: WorkItemSummary["source"]): WorkItemSummary {
  const fields = item.fields ?? {};
  return {
    id: item.id,
    type: str(fields["System.WorkItemType"]) || "Work Item",
    title: str(fields["System.Title"]) || `Work item ${item.id}`,
    state: str(fields["System.State"]),
    source,
  };
}

/**
 * Configuration baked into the extension package (`dist/agent-config.json`).
 *
 * This is the administrator-controlled channel: changing it means repackaging
 * and reinstalling the extension, which requires the organization-level "Manage
 * extensions" permission. Ordinary users cannot write it. `projects` keys are
 * project names, matched case-insensitively, so one package can serve several
 * projects with different agents.
 */
interface PackagedConfig {
  default?: Partial<HubSettings>;
  projects?: Record<string, Partial<HubSettings>>;
}

const PACKAGED_CONFIG_FILE = "agent-config.json";

function settingsFrom(value: Partial<HubSettings> | undefined): HubSettings {
  return {
    agentUrl: str(value?.agentUrl).trim().replace(/\/+$/, ""),
    entraClientId: str(value?.entraClientId).trim(),
    entraTenantId: str(value?.entraTenantId).trim(),
    agentScope: str(value?.agentScope).trim(),
  };
}

class AdoHost implements HubHost {
  private context: HubContext = { organization: "", project: "", userDisplayName: "" };
  private projectId = "";
  private collectionUrl = "";
  private settings: HubSettings = { ...EMPTY_SETTINGS };
  private msal: IPublicClientApplication | undefined;
  private msalClientId = "";

  async initialize(): Promise<HubContext> {
    // applyTheme:true makes the host inject its palette as CSS custom properties
    // on :root, which is what styles.css is written against. loaded:false lets us
    // finish the first render before the host reveals the frame.
    await SDK.init({ loaded: false, applyTheme: true });
    await SDK.ready();

    const host = SDK.getHost();
    const user = SDK.getUser();

    let projectName = "";
    try {
      const projectService = await SDK.getService<IProjectPageService>(PROJECT_PAGE_SERVICE);
      const project = await projectService.getProject();
      projectName = project?.name ?? "";
      this.projectId = project?.id ?? "";
    } catch {
      // Fall through to the web context below.
    }
    if (!projectName) {
      const webContext = SDK.getWebContext();
      projectName = webContext?.project?.name ?? "";
      this.projectId = this.projectId || (webContext?.project?.id ?? "");
    }

    this.collectionUrl = `https://dev.azure.com/${encodeURIComponent(host.name)}`;

    this.context = {
      organization: host.name,
      project: projectName,
      userDisplayName: user?.displayName ?? "",
    };
    return this.context;
  }

  notifyReady(): void {
    void SDK.notifyLoadSucceeded();
    SDK.resize();
  }

  // ---------------------------------------------------------------- settings

  private async dataManager(): Promise<IExtensionDataManager> {
    const service = await SDK.getService<IExtensionDataService>(EXTENSION_DATA_SERVICE);
    const extensionId = SDK.getExtensionContext().id;
    const token = await SDK.getAccessToken();
    return service.getExtensionDataManager(extensionId, token);
  }

  /**
   * The extension data service scopes documents to the whole organization
   * ("Default") or to a single user ("User") — there is no project scope, and no
   * per-document ACL on the organization-wide scope. Every user of the hub holds
   * `vso.extension.data_write`, so a document written at "Default" scope is
   * writable by any member of the organization; since these settings name both
   * the host this page sends an Entra token to and the audience it requests that
   * token for, an organization-wide writable document is a token-redirection
   * primitive.
   *
   * So the shared, organization-wide settings document is gone. Settings come
   * from one of two places, in order:
   *
   *   1. `dist/agent-config.json` in the extension package — administrator
   *      controlled, because changing it requires repackaging and reinstalling.
   *   2. A "User" scoped document, which only its own owner can read or write.
   *
   * The key is still namespaced by project id so one user can point different
   * projects at different agents.
   */
  private settingsKey(): string {
    return `settings.${this.projectId || this.context.project || "default"}`;
  }

  /** Administrator-pinned settings from the package, if any and if valid. */
  private async packagedSettings(): Promise<HubSettings | undefined> {
    let config: PackagedConfig;
    try {
      const response = await fetch(PACKAGED_CONFIG_FILE, { headers: { Accept: "application/json" } });
      if (!response.ok) return undefined;
      config = (await response.json()) as PackagedConfig;
    } catch {
      // No packaged configuration (the usual case): fall back to user settings.
      return undefined;
    }
    if (typeof config !== "object" || config === null) return undefined;

    const byProject = config.projects ?? {};
    const projectKey = Object.keys(byProject).find(
      (name) => name.toLowerCase() === this.context.project.toLowerCase(),
    );
    const merged = settingsFrom({
      ...(config.default ?? {}),
      ...(projectKey ? byProject[projectKey] : {}),
    });
    // A half-filled or malformed packaged config is ignored rather than half-used.
    return validateSettings(merged).valid ? merged : undefined;
  }

  async loadSettings(): Promise<LoadedSettings> {
    const packaged = await this.packagedSettings();
    if (packaged) {
      this.settings = packaged;
      return { settings: packaged, managed: true };
    }

    let stored: Partial<HubSettings> | undefined;
    try {
      const manager = await this.dataManager();
      stored = await manager.getValue<Partial<HubSettings> | undefined>(this.settingsKey(), {
        scopeType: "User",
        defaultValue: undefined,
      });
    } catch {
      stored = undefined;
    }
    // Stored values are data, not truth: anything that would not pass the
    // settings form is discarded here rather than carried as far as a token
    // request. `agentFetch` re-checks, so this is defence in depth, not the gate.
    const candidate = settingsFrom(stored);
    this.settings = validateSettings(candidate).valid ? candidate : { ...EMPTY_SETTINGS };
    return { settings: this.settings, managed: false };
  }

  async saveSettings(settings: HubSettings): Promise<void> {
    const check = validateSettings(settings);
    if (!check.valid) {
      throw new AgentRequestError(check.message ?? "These settings are not valid.", {
        retryable: false,
      });
    }
    const manager = await this.dataManager();
    await manager.setValue(this.settingsKey(), settings, { scopeType: "User" });
    if (settings.entraClientId !== this.msalClientId) {
      // Force a new MSAL instance on the next token request.
      this.msal = undefined;
    }
    this.settings = settings;
  }

  // ------------------------------------------------------------- ADO queries

  private async adoFetch<T>(path: string, init?: RequestInit): Promise<T> {
    const token = await SDK.getAccessToken();
    const response = await fetch(`${this.collectionUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
    });
    if (!response.ok) {
      throw new Error(`Azure DevOps returned ${response.status} for ${path.split("?")[0]}`);
    }
    return (await response.json()) as T;
  }

  private get projectSegment(): string {
    return encodeURIComponent(this.context.project);
  }

  async listMyWorkItems(): Promise<WorkItemSummary[]> {
    const [assigned, recent] = await Promise.all([
      this.assignedWorkItems().catch(() => [] as WorkItemSummary[]),
      this.recentWorkItems().catch(() => [] as WorkItemSummary[]),
    ]);

    const seen = new Set<number>();
    const merged: WorkItemSummary[] = [];
    for (const item of [...assigned, ...recent]) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      merged.push(item);
    }
    return merged;
  }

  private async assignedWorkItems(): Promise<WorkItemSummary[]> {
    const query =
      "SELECT [System.Id] FROM WorkItems" +
      " WHERE [System.AssignedTo] = @Me" +
      " AND [System.State] NOT IN ('Closed', 'Removed', 'Done', 'Completed', 'Resolved')" +
      " ORDER BY [System.ChangedDate] DESC";
    const result = await this.adoFetch<WiqlResponse>(
      `/${this.projectSegment}/_apis/wit/wiql?$top=${MAX_ASSIGNED}&api-version=${API_VERSION}`,
      { method: "POST", body: JSON.stringify({ query }) },
    );
    const ids = (result.workItems ?? []).map((w) => w.id).slice(0, MAX_ASSIGNED);
    if (ids.length === 0) return [];
    return this.workItemsByIds(ids, "assigned");
  }

  private async recentWorkItems(): Promise<WorkItemSummary[]> {
    const activity = await this.adoFetch<AdoListResponse<AdoRecentActivity>>(
      `/_apis/work/accountmyworkrecentactivity?api-version=${API_VERSION}`,
    );
    return (activity.value ?? [])
      .filter((item) => !item.teamProject || item.teamProject === this.context.project)
      .slice(0, MAX_RECENT)
      .map((item) => ({
        id: item.id,
        type: item.workItemType || "Work Item",
        title: item.title || `Work item ${item.id}`,
        state: item.state ?? "",
        source: "recent" as const,
      }));
  }

  private async workItemsByIds(
    ids: number[],
    source: WorkItemSummary["source"],
  ): Promise<WorkItemSummary[]> {
    const result = await this.adoFetch<AdoListResponse<AdoWorkItem>>(
      `/${this.projectSegment}/_apis/wit/workitems` +
        `?ids=${ids.join(",")}` +
        `&fields=${WORK_ITEM_FIELDS.join(",")}` +
        `&api-version=${API_VERSION}`,
    );
    const byId = new Map(result.value.map((item) => [item.id, item]));
    // Preserve the WIQL ordering rather than the batch endpoint's ordering.
    return ids
      .map((id) => byId.get(id))
      .filter((item): item is AdoWorkItem => Boolean(item))
      .map((item) => toWorkItem(item, source));
  }

  async findWorkItemById(id: number): Promise<WorkItemSummary | undefined> {
    try {
      const item = await this.adoFetch<AdoWorkItem>(
        `/${this.projectSegment}/_apis/wit/workitems/${id}` +
          `?fields=${WORK_ITEM_FIELDS.join(",")}` +
          `&api-version=${API_VERSION}`,
      );
      return toWorkItem(item, "recent");
    } catch {
      return undefined;
    }
  }

  async listRepositories(): Promise<RepositorySummary[]> {
    const result = await this.adoFetch<AdoListResponse<AdoRepository>>(
      `/${this.projectSegment}/_apis/git/repositories?api-version=${API_VERSION}`,
    );
    return result.value
      .filter((repo) => !repo.isDisabled)
      .map((repo) => ({
        id: repo.id,
        name: repo.name,
        defaultBranch: repo.defaultBranch,
        isDisabled: repo.isDisabled,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  // ----------------------------------------------------------- agent + MSAL

  /**
   * Public client with PKCE. There is no client secret here and there must
   * never be one: everything in this package is served to the browser.
   *
   * `createNestablePublicClientApplication` uses the Azure DevOps host frame's
   * Nested App Auth bridge when it is available, which avoids third-party
   * cookie and cross-origin popup problems inside the extension iframe, and
   * falls back to a standard public client otherwise. `redirectUri` is
   * deliberately unset — the NAA bridge supplies it.
   */
  private async getMsal(): Promise<IPublicClientApplication> {
    if (this.msal && this.msalClientId === this.settings.entraClientId) {
      return this.msal;
    }
    if (!this.settings.entraClientId) {
      throw new AgentRequestError("No Entra client id is configured for this project.", {
        retryable: false,
      });
    }
    try {
      await SDK.enableNestedAppAuth();
    } catch {
      // Host does not support the NAA bridge; MSAL falls back to a standard
      // public client below.
    }
    this.msal = await createNestablePublicClientApplication({
      auth: {
        clientId: this.settings.entraClientId,
        // Never the default `/common`: a single-tenant registration refuses it with
        // AADSTS50194, and naming the directory keeps sign-in to this tenant.
        authority: entraAuthority(this.settings.entraTenantId),
      },
      cache: { cacheLocation: "sessionStorage" },
    });
    this.msalClientId = this.settings.entraClientId;
    return this.msal;
  }

  /**
   * Returns a bearer token for the Agent Application. The value never leaves
   * this class: callers use `agentFetch`, not the token itself.
   *
   * `allowInteractive` is false for everything on the polling path. Polling is
   * not a user gesture: a popup raised from it is unexpected, and one raised
   * every five seconds for the length of a run is abuse. If the silent path
   * cannot produce a token while a run is in flight, that is terminal for the
   * poll loop — the user re-submits, and *that* is where a popup is allowed.
   */
  private async getAgentToken(allowInteractive: boolean): Promise<string> {
    const msal = await this.getMsal();
    const scopes = [this.settings.agentScope];
    const accounts = msal.getAllAccounts();
    const account: AccountInfo | undefined = accounts[0];

    if (account) {
      try {
        const silent = await msal.acquireTokenSilent({ scopes, account });
        return silent.accessToken;
      } catch (error) {
        if (!allowInteractive) throw signInRequired();
        if (!(error instanceof InteractionRequiredAuthError)) throw error;
      }
    } else if (!allowInteractive) {
      throw signInRequired();
    }
    const interactive = await msal.acquireTokenPopup({ scopes });
    return interactive.accessToken;
  }

  private async agentFetch(
    path: string,
    options: { allowInteractive: boolean; init?: RequestInit },
  ): Promise<unknown> {
    // Before the token, not after: this is the check that decides which origin
    // is about to receive an Entra access token, so it runs before one exists.
    // Settings can reach this class from storage as well as from the form.
    const check = validateSettings(this.settings);
    if (!check.valid) {
      throw new AgentRequestError(
        `The agent settings for this project are not usable, so no token was requested. ${check.message ?? ""}`.trim(),
        { retryable: false },
      );
    }

    const token = await this.getAgentToken(options.allowInteractive);
    let response: Response;
    try {
      response = await fetch(agentEndpoint(this.settings.agentUrl, path), {
        ...options.init,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(options.init?.headers ?? {}),
          Authorization: `Bearer ${token}`,
        },
      });
    } catch {
      throw new AgentRequestError(
        "Could not reach the agent. Check the Agent Application URL in settings and that the service is running.",
      );
    }

    if (!response.ok) {
      // allowInteractive is true on the submit path and false on the poll path,
      // which is exactly the distinction describeHttpFailure needs for a 404.
      throw new AgentRequestError(await describeHttpFailure(response, options.allowInteractive), {
        status: response.status,
      });
    }
    try {
      return (await response.json()) as unknown;
    } catch {
      throw new AgentRequestError(
        "The agent replied with something that is not JSON. Check that the Agent Application URL points at the agent and not at a proxy or sign-in page.",
        { status: response.status, retryable: false },
      );
    }
  }

  async submitRun(request: CreateRunRequest): Promise<CreateRunResponse> {
    const payload = await this.agentFetch("/invocations", {
      allowInteractive: true,
      init: { method: "POST", body: JSON.stringify(request) },
    });
    if (!isCreateRunResponse(payload)) {
      throw new AgentRequestError(
        "The agent accepted the run but its reply did not contain a run id, so there is nothing to follow. Check the Agent Application URL and the agent's version.",
        { retryable: false },
      );
    }
    return payload;
  }

  async getRunStatus(runId: string): Promise<RunStatus> {
    const payload = await this.agentFetch(`/invocations/${encodeURIComponent(runId)}`, {
      allowInteractive: false,
    });
    if (!isRunStatus(payload)) {
      throw new AgentRequestError(
        "The agent replied with a run status this version of the extension does not understand, so the run cannot be followed here. Check the repository for a new pull request.",
        { retryable: false },
      );
    }
    return payload;
  }

  openUrl(url: string): void {
    void SDK.getService<IHostNavigationService>(HOST_NAVIGATION_SERVICE)
      .then((service) => service.openNewWindow(url, ""))
      .catch(() => {
        window.open(url, "_blank", "noopener,noreferrer");
      });
  }
}

/**
 * Raised when a token is needed but only an interactive sign-in could produce
 * one, on a code path where interaction is not allowed (polling).
 */
function signInRequired(): AgentRequestError {
  return new AgentRequestError(
    "Your sign-in for the agent needs renewing, and this page will not open a sign-in window while a run is being followed. Submit the run again to sign in — the run itself is unaffected and may still be in progress.",
    { status: 401, retryable: false },
  );
}

/**
 * Turns a non-2xx agent response into a sentence. Note this covers transport
 * and auth failures only — a run that starts and then fails comes back as a
 * 200 with a contract error code, which errors.ts handles.
 *
 * `submitting` distinguishes the two callers, because the same status does not
 * mean the same thing on both: a 404 from POST /invocations is a wrong base URL,
 * a 404 from GET /invocations/{id} is an unknown run id.
 */
async function describeHttpFailure(response: Response, submitting: boolean): Promise<string> {
  if (response.status === 401) {
    return "The agent rejected your sign-in. The token is issued for https://ai.azure.com, the audience a Microsoft Foundry hosted agent expects, so check that the Agent Application URL really is your Foundry agent endpoint.";
  }
  if (response.status === 403) {
    return "You are not allowed to invoke this agent. Ask an administrator to grant you the Foundry Agent Consumer role (previously named Azure AI User) on the Foundry project or on this agent.";
  }
  if (response.status === 404) {
    // On the polling path a 404 is the agent saying it has no such run — a
    // restarted agent loses its in-memory runs — not a wrong base URL.
    return submitting
      ? "The agent URL returned 404. Check the Agent Application URL in settings."
      : "The agent does not know about this run any more, so its progress cannot be followed here. It may have restarted; check the repository for a new pull request.";
  }
  let detail = "";
  try {
    const body = (await response.json()) as { message?: string; error?: { message?: string } };
    detail = body.error?.message ?? body.message ?? "";
  } catch {
    // Non-JSON error body; the status code alone will have to do.
  }
  if (response.status === 400) {
    return detail
      ? `The agent rejected the request: ${detail}`
      : "The agent rejected the request as invalid.";
  }
  return detail
    ? `The agent returned an error (${response.status}): ${detail}`
    : `The agent returned an error (${response.status}).`;
}

export function createAdoHost(): HubHost {
  return new AdoHost();
}
