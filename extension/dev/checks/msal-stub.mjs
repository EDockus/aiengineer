/*
 * Stands in for `@azure/msal-browser`. Counts every interactive call, which is
 * the whole point of the poll-path check: a popup raised from a background poll
 * is the defect, so the assertion is a call count of zero.
 */
export const state = {
  accounts: [],
  popupCalls: [],
  silentCalls: [],
  /** Error thrown by acquireTokenSilent, if any. */
  silentError: undefined,
  accessToken: "agent-access-token",
};

export function reset() {
  state.accounts = [];
  state.popupCalls = [];
  state.silentCalls = [];
  state.silentError = undefined;
}

export class InteractionRequiredAuthError extends Error {
  constructor(message = "interaction_required") {
    super(message);
    this.name = "InteractionRequiredAuthError";
  }
}

export async function createNestablePublicClientApplication(config) {
  return {
    config,
    getAllAccounts: () => state.accounts,
    async acquireTokenSilent(request) {
      state.silentCalls.push(request);
      if (state.silentError) throw state.silentError;
      return { accessToken: state.accessToken };
    },
    async acquireTokenPopup(request) {
      state.popupCalls.push(request);
      return { accessToken: state.accessToken };
    },
  };
}
