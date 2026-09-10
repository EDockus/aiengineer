/*
 * Stands in for `azure-devops-extension-sdk` when checking src/ado-host.ts under
 * Node. Aliased in by scripts/verify.mjs, so ado-host.ts itself is unmodified
 * and unaware.
 *
 * `calls` records everything the host asked the SDK for, and `documents` is the
 * extension data store, keyed by "<scopeType>:<key>" so a check can assert which
 * scope a settings document was written at.
 */
export const state = {
  calls: [],
  documents: new Map(),
  accessToken: "ado-sdk-token",
  host: { name: "contoso" },
  user: { displayName: "Dev Harness User" },
  project: { id: "project-guid", name: "Platform" },
  /** Set to make getService reject, as an unavailable service would. */
  failService: false,
};

export function reset() {
  state.calls = [];
  state.documents = new Map();
  state.failService = false;
}

const record = (name, detail) => state.calls.push({ name, detail });

export async function init() {
  record("init");
}
export async function ready() {
  record("ready");
}
export async function notifyLoadSucceeded() {
  record("notifyLoadSucceeded");
}
export function resize() {
  record("resize");
}
export function getHost() {
  return state.host;
}
export function getUser() {
  return state.user;
}
export function getWebContext() {
  return { project: state.project };
}
export function getExtensionContext() {
  return { id: "publisher.ticket-to-pr", publisherId: "publisher", extensionId: "ticket-to-pr" };
}
export async function getAccessToken() {
  record("getAccessToken");
  return state.accessToken;
}
export async function enableNestedAppAuth() {
  record("enableNestedAppAuth");
}

const dataManager = {
  async getValue(key, options) {
    record("data.getValue", { key, scopeType: options?.scopeType });
    const stored = state.documents.get(`${options?.scopeType}:${key}`);
    return stored === undefined ? options?.defaultValue : stored;
  },
  async setValue(key, value, options) {
    record("data.setValue", { key, scopeType: options?.scopeType, value });
    state.documents.set(`${options?.scopeType}:${key}`, value);
    return value;
  },
};

export async function getService(contributionId) {
  record("getService", contributionId);
  if (state.failService) throw new Error("service unavailable");
  if (contributionId === "ms.vss-features.extension-data-service") {
    return { getExtensionDataManager: async () => dataManager };
  }
  if (contributionId === "ms.vss-tfs-web.tfs-page-data-service") {
    return { getProject: async () => state.project };
  }
  return {};
}
