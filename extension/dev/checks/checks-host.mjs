/*
 * Runtime checks for src/ado-host.ts — the real host implementation, with the
 * Azure DevOps SDK and MSAL aliased to local stubs and `fetch` replaced. Nothing
 * in src/ is modified or aware of this.
 */
import { AgentRequestError, createAdoHost } from "../../src/ado-host.ts";
import { validateAgentUrl } from "../../src/host.ts";
import * as sdk from "./sdk-stub.mjs";
import * as msal from "./msal-stub.mjs";
import { assert, assertEqual, assertIncludes, check, note, rejects } from "./harness.mjs";

const GOOD = {
  agentUrl: "https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols",
  entraClientId: "11111111-2222-3333-4444-555555555555",
  entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  agentScope: "https://ai.azure.com/.default",
};

const SETTINGS_KEY = "settings.project-guid";

/** Replaces global fetch; records every request the host makes. */
function stubFetch(responder) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return responder(String(url), init, calls.length);
  };
  return calls;
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      if (typeof body === "string") throw new SyntaxError("not json");
      return body;
    },
  };
}

/** A configured, initialized host with no packaged config file present. */
async function bootHost({ stored = GOOD, packaged = undefined } = {}) {
  sdk.reset();
  msal.reset();
  if (stored) {
    sdk.state.documents.set(`User:${SETTINGS_KEY}`, stored);
    // Seeded at the organization-wide scope too, so that a build which reads
    // "Default" finds the same document and these checks stay meaningful when
    // run against the pre-fix code (VERIFY_SRC).
    sdk.state.documents.set(`Default:${SETTINGS_KEY}`, stored);
  }

  const configCalls = [];
  globalThis.fetch = async (url) => {
    configCalls.push(String(url));
    if (String(url).endsWith("agent-config.json")) {
      return packaged ? jsonResponse(packaged) : jsonResponse(null, 404);
    }
    throw new Error(`unexpected fetch of ${url}`);
  };

  const host = createAdoHost();
  await host.initialize();
  const loaded = await host.loadSettings();
  return { host, loaded, configCalls };
}

export async function run() {
  // ----------------------------------------------------------------- defect 2
  await check("defect 2: polling never opens a sign-in popup when no account is cached", async () => {
    const { host } = await bootHost();
    msal.state.accounts = []; // nothing signed in yet
    const calls = stubFetch(() => jsonResponse({ runId: "run-1", status: "running", phase: "coding" }));

    const error = await rejects(host.getRunStatus("run-1"), "getRunStatus refuses to sign in silently");
    assertEqual(msal.state.popupCalls.length, 0, "acquireTokenPopup was called zero times");
    assertEqual(calls.length, 0, "and no request was sent to the agent");
    assert(error instanceof AgentRequestError, "it is an AgentRequestError");
    assertEqual(error.retryable, false, "marked non-retryable, so the poll loop stops");
    assertEqual(error.status, 401, "reported as an auth failure");
  });

  await check("defect 2: an expired silent token during polling is terminal, not a popup", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a", username: "user@contoso.com" }];
    msal.state.silentError = new msal.InteractionRequiredAuthError();
    stubFetch(() => jsonResponse({ runId: "run-1", status: "running", phase: "coding" }));

    const error = await rejects(host.getRunStatus("run-1"), "getRunStatus does not fall back to a popup");
    assertEqual(msal.state.popupCalls.length, 0, "acquireTokenPopup was called zero times");
    assertEqual(error.retryable, false, "non-retryable");
  });

  await check("defect 2: submit — a user gesture — may still sign in interactively", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [];
    stubFetch(() => jsonResponse({ runId: "run-1", status: "queued" }));

    const accepted = await host.submitRun({
      version: "1",
      organization: "contoso",
      project: "Platform",
      workItem: { type: "Bug", id: 4123 },
      repository: "payments-api",
      branch: "bug/4123-x",
    });
    assertEqual(accepted.runId, "run-1", "the run was submitted");
    assertEqual(msal.state.popupCalls.length, 1, "exactly one interactive sign-in, from the submit path");
  });

  // ----------------------------------------------------------------- defect 4
  await check("defect 4: an http:// URL in storage is dropped on load and blocks any token", async () => {
    const { host, loaded } = await bootHost({
      stored: { ...GOOD, agentUrl: "http://evil.test" },
    });
    assertEqual(loaded.settings.agentUrl, "", "loadSettings fell back to empty settings");

    const calls = stubFetch(() => jsonResponse({ runId: "x", status: "queued" }));
    const error = await rejects(host.submitRun({ version: "1" }), "submitRun refuses to run");
    assertEqual(msal.state.silentCalls.length + msal.state.popupCalls.length, 0, "no token was acquired");
    assertEqual(calls.length, 0, "and nothing was sent anywhere");
    assertEqual(error.retryable, false, "a configuration failure is not retried");
  });

  await check("defect 4: a userinfo URL in storage is dropped and blocks any token", async () => {
    const { host, loaded } = await bootHost({
      stored: { ...GOOD, agentUrl: "https://contoso.services.ai.azure.com@evil.test" },
    });
    assertEqual(loaded.settings.agentUrl, "", "loadSettings fell back to empty settings");
    const calls = stubFetch(() => jsonResponse({}));
    await rejects(host.submitRun({ version: "1" }), "submitRun refuses to run");
    assertEqual(msal.state.silentCalls.length + msal.state.popupCalls.length, 0, "no token was acquired");
    assertEqual(calls.length, 0, "nothing was sent");
    assert(
      !validateAgentUrl("https://contoso.services.ai.azure.com@evil.com/").valid,
      "and the validator rejects it directly",
    );
    assert(!validateAgentUrl(`${GOOD.agentUrl}?x=1`).valid, "query strings are rejected");
    assert(!validateAgentUrl(`${GOOD.agentUrl}#f`).valid, "fragments are rejected");
    assert(validateAgentUrl(GOOD.agentUrl).valid, "a Foundry https base URL is accepted");
  });

  await check("defect 4: the guard runs before the token even with settings set in memory", async () => {
    // Simulates a store that changes under the page: valid at load, hostile after.
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a" }];
    stubFetch(() => jsonResponse({ runId: "r", status: "queued" }));
    await host.saveSettings(GOOD);
    const poisoned = { ...GOOD, agentScope: "https://graph.microsoft.com/.default" };
    await rejects(host.saveSettings(poisoned), "saveSettings rejects a cross-audience scope");
    const stored = sdk.state.documents.get(`User:${SETTINGS_KEY}`);
    assertEqual(stored.agentScope, GOOD.agentScope, "the stored document still holds the Foundry scope");
  });

  // ----------------------------------------------------------------- defect 5
  await check("defect 5: settings are written at User scope, never organization-wide", async () => {
    const { host } = await bootHost();
    await host.saveSettings(GOOD);
    const writes = sdk.state.calls.filter((c) => c.name === "data.setValue");
    const reads = sdk.state.calls.filter((c) => c.name === "data.getValue");
    assertEqual(writes.length, 1, "one settings write");
    assertEqual(writes[0].detail.scopeType, "User", "at User scope");
    assert(
      reads.every((r) => r.detail.scopeType === "User"),
      "and every read is at User scope too",
    );
    assert(
      !sdk.state.calls.some((c) => c.detail && c.detail.scopeType === "Default"),
      "nothing touches the organization-wide Default scope",
    );
  });

  await check("defect 5: a cross-audience scope in storage is ignored on load", async () => {
    const { loaded } = await bootHost({
      stored: { ...GOOD, agentScope: "https://graph.microsoft.com/.default" },
    });
    assertEqual(loaded.settings.agentScope, "", "the Graph scope never becomes live settings");
    assertEqual(loaded.settings.agentUrl, "", "the whole document is discarded, not half-used");
  });

  await check("defect 5: packaged configuration wins and reports itself as managed", async () => {
    const { loaded } = await bootHost({
      stored: { ...GOOD, agentUrl: "https://user-set.services.ai.azure.com/api/projects/p" },
      packaged: {
        default: GOOD,
        projects: { platform: { agentUrl: "https://platform.services.ai.azure.com/api/projects/p" } },
      },
    });
    assertEqual(loaded.managed, true, "reported as managed, so the form is read-only");
    assertEqual(
      loaded.settings.agentUrl,
      "https://platform.services.ai.azure.com/api/projects/p",
      "the project entry wins",
    );
    assertEqual(loaded.settings.agentScope, GOOD.agentScope, "other fields come from the default block");
  });

  await check("defect 5: a malformed packaged configuration is ignored, not half-used", async () => {
    const { loaded } = await bootHost({
      stored: GOOD,
      packaged: { default: { agentUrl: "https://packaged.services.ai.azure.com/api/projects/p" } },
    });
    assertEqual(loaded.managed, false, "not treated as managed");
    assertEqual(loaded.settings.agentUrl, GOOD.agentUrl, "the user's own settings are used instead");
  });

  // ----------------------------------------------------------------- defect 6
  await check("defect 6: a 202 with no runId is rejected instead of polling /invocations/undefined", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a" }];
    const calls = stubFetch(() => jsonResponse({}, 202));
    const error = await rejects(host.submitRun({ version: "1" }), "submitRun rejects an empty body");
    assertIncludes(error.message, "did not contain a run id", "and says why");
    assertEqual(error.retryable, false, "non-retryable");
    assertEqual(calls.length, 1, "exactly one request was made");
    assert(
      !calls.some((c) => c.url.includes("undefined")),
      "no request was ever made for /invocations/undefined",
    );
  });

  await check("defect 6: an unknown status or phase is rejected", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a" }];

    stubFetch(() => jsonResponse({ runId: "x", status: "weird" }));
    const bad = await rejects(host.getRunStatus("x"), 'status "weird" is rejected');
    assertEqual(bad.retryable, false, "non-retryable");

    stubFetch(() => jsonResponse({ runId: "x", status: "running", phase: "nonsense" }));
    await rejects(host.getRunStatus("x"), 'phase "nonsense" is rejected');

    stubFetch(() => jsonResponse({ runId: "", status: "running" }));
    await rejects(host.getRunStatus("x"), "an empty runId is rejected");

    stubFetch(() =>
      jsonResponse({ runId: "x", status: "succeeded", phase: "done", result: { pullRequestId: 1 } }),
    );
    await rejects(host.getRunStatus("x"), "a truncated result object is rejected");

    stubFetch(() =>
      jsonResponse({
        runId: "x",
        status: "succeeded",
        phase: "done",
        startedAt: "2026-01-01T00:00:00Z",
        finishedAt: null,
        result: {
          pullRequestId: 812,
          pullRequestUrl: "https://dev.azure.com/o/p/_git/r/pullrequest/812",
          branch: "bug/1-x",
          commits: 3,
          filesChanged: 7,
        },
      }),
    );
    const good = await host.getRunStatus("x");
    assertEqual(good.status, "succeeded", "a well-formed status still passes through unchanged");
  });

  await check("defect 6: a non-JSON body is a sentence, not a crash", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a" }];
    stubFetch(() => jsonResponse("<html>sign in</html>", 200));
    const error = await rejects(host.getRunStatus("x"), "an HTML body is rejected");
    assertIncludes(error.message, "not JSON", "with an actionable sentence");
  });

  // ------------------------------------------------------- status classification
  await check("defect 1: HTTP statuses are classified retryable or terminal", async () => {
    const { host } = await bootHost();
    msal.state.accounts = [{ homeAccountId: "a" }];
    const seen = [];
    for (const status of [401, 403, 404, 400, 429, 500, 503]) {
      stubFetch(() => jsonResponse({ message: "no" }, status));
      const error = await rejects(host.getRunStatus("x"), `HTTP ${status} throws`).catch(() => undefined);
      seen.push(`${status}:${error && error.retryable ? "retry" : "stop"}`);
    }
    note(`classification: ${seen.join(" ")}`);
    assertEqual(
      seen.join(" "),
      "401:stop 403:stop 404:stop 400:stop 429:retry 500:retry 503:retry",
      "4xx stops the poll loop, 429/5xx keeps trying",
    );

    // Same status, different meaning depending on which call made it.
    stubFetch(() => jsonResponse({}, 404));
    const polled = await rejects(host.getRunStatus("x"), "a 404 while polling");
    assertIncludes(polled.message, "does not know about this run", "reads as an unknown run id");
    const posted = await rejects(host.submitRun({ version: "1" }), "a 404 while submitting");
    assertIncludes(posted.message, "Check the Agent Application URL", "reads as a wrong base URL");
  });
}
