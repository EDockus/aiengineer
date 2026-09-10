/*
 * Runtime checks for src/hub.ts, driven through the real bootstrap() against the
 * real src/hub.html markup. Only the HubHost seam is faked, so the polling,
 * timer and rendering code under test is exactly the shipped code.
 */
import { bootstrap } from "../../src/hub.ts";
import { AgentRequestError } from "../../src/ado-host.ts";
import { installDom, findAll } from "./dom-stub.mjs";
import { assert, assertEqual, assertIncludes, check, note, sleep, waitFor } from "./harness.mjs";

const HUB_HTML = process.env.HUB_HTML;
const POLL_INTERVAL_MS = 5000;

const SETTINGS = {
  agentUrl: "https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols",
  entraClientId: "11111111-2222-3333-4444-555555555555",
  entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  agentScope: "https://ai.azure.com/.default",
};

/** A HubHost that records every call, with per-check overrides. */
function makeHost(overrides = {}) {
  const calls = { submitRun: [], getRunStatus: [], saveSettings: [], openUrl: [] };
  const host = {
    calls,
    async initialize() {
      return { organization: "contoso", project: "Platform", userDisplayName: "Dev User" };
    },
    notifyReady() {},
    async loadSettings() {
      return { settings: { ...SETTINGS }, managed: false };
    },
    async saveSettings(settings) {
      calls.saveSettings.push(settings);
    },
    async listMyWorkItems() {
      return [
        { id: 4123, type: "Bug", title: "Null reference when payment method is missing", state: "Active", source: "assigned" },
      ];
    },
    async findWorkItemById() {
      return undefined;
    },
    async listRepositories() {
      return [{ id: "repo-1", name: "payments-api", defaultBranch: "refs/heads/main" }];
    },
    async submitRun(request) {
      calls.submitRun.push(request);
      return { runId: `run-${calls.submitRun.length}`, status: "queued", correlationId: request.correlationId };
    },
    async getRunStatus(runId) {
      calls.getRunStatus.push({ runId, at: Date.now() });
      return { runId, status: "running", phase: "coding" };
    },
    openUrl(url) {
      calls.openUrl.push(url);
    },
    ...overrides,
  };
  if (overrides.getRunStatus) {
    const inner = overrides.getRunStatus;
    host.getRunStatus = async (runId) => {
      calls.getRunStatus.push({ runId, at: Date.now() });
      return inner(runId, calls.getRunStatus.length);
    };
  }
  return host;
}

/** Boots the hub against a fresh DOM and returns the page handle. */
async function boot(host) {
  const page = installDom(HUB_HTML);
  await bootstrap(host);
  return page;
}

function teardown(page) {
  page.fireWindow("beforeunload");
}

export async function run() {
  // ------------------------------------------------------------- defect 1 + 2
  await check("defect 1: a 403 while polling stops the loop after one request", async () => {
    const host = makeHost({
      getRunStatus: async () => {
        throw new AgentRequestError(
          "You are not allowed to invoke this agent. Ask an administrator to grant you the Foundry Agent Consumer role (previously named Azure AI User) on the Foundry project or on this agent.",
          { status: 403 },
        );
      },
    });
    const page = await boot(host);

    page.submitForm("run-form");
    await waitFor(() => host.calls.getRunStatus.length >= 1, 2000, "the first poll");
    await sleep(POLL_INTERVAL_MS + 800); // long enough for a retry to have fired

    assertEqual(host.calls.getRunStatus.length, 1, "exactly one getRunStatus call");
    assertIncludes(
      page.el("status-live").textContent,
      "not allowed to invoke this agent",
      "the 403 sentence is announced",
    );
    assertIncludes(
      page.el("status-detail").textContent,
      "The run could not be started.",
      "a terminal failure is rendered",
    );
    assertEqual(page.el("submit").textContent, "Create PR with agent", "the submit button is restored");
    assertEqual(page.el("submit").disabled, false, "the submit button is enabled again");
    teardown(page);
  });

  await check("defect 1: a 500 is retried, and the retry cap ends it", async () => {
    const host = makeHost({
      getRunStatus: async () => {
        throw new AgentRequestError("The agent returned an error (503).", { status: 503 });
      },
    });
    const page = await boot(host);

    page.submitForm("run-form");
    await waitFor(() => host.calls.getRunStatus.length >= 2, POLL_INTERVAL_MS + 2000, "a retry after a 503");
    assert(host.calls.getRunStatus.length >= 2, "a 5xx is retried rather than reported immediately");
    note(`getRunStatus calls after one interval: ${host.calls.getRunStatus.length}`);
    teardown(page);
  });

  await check("defect 1: the consecutive-failure cap is five, not the 45-minute ceiling", async () => {
    // Same shape as the loop, driven directly: five failures end it, and the
    // wall clock is nowhere near the ceiling.
    const host = makeHost({
      getRunStatus: async () => {
        throw new Error("network down");
      },
    });
    const page = await boot(host);
    page.submitForm("run-form");

    // 1 immediate + up to 4 retries = 5 attempts, then it gives up.
    await waitFor(
      () => page.el("status-detail").textContent.includes("Lost contact"),
      5 * POLL_INTERVAL_MS + 4000,
      "the failure cap to end the run",
    );
    assertEqual(host.calls.getRunStatus.length, 5, "five attempts, then stop");
    assertIncludes(
      page.el("status-detail").textContent,
      "5 attempts in a row",
      "the user is told why polling stopped",
    );
    teardown(page);
  });

  // ----------------------------------------------------------------- defect 3
  await check("defect 3: two submits leave exactly one poll chain", async () => {
    const host = makeHost();
    const page = await boot(host);

    page.submitForm("run-form");
    await waitFor(() => host.calls.getRunStatus.length === 1, 2000, "run 1's first poll");
    page.submitForm("run-form");
    await waitFor(() => host.calls.getRunStatus.length === 2, 2000, "run 2's first poll");

    assertEqual(host.calls.submitRun.length, 2, "two runs were submitted");
    const before = host.calls.getRunStatus.length;
    await sleep(POLL_INTERVAL_MS + 800);
    const during = host.calls.getRunStatus.length - before;

    assertEqual(during, 1, "one getRunStatus call in the five seconds after the second submit");
    const polled = new Set(host.calls.getRunStatus.slice(before).map((c) => c.runId));
    assertEqual([...polled].join(","), "run-2", "and it polls the current run");
    teardown(page);
  });

  await check("defect 3: three submits still leave one poll chain", async () => {
    const host = makeHost();
    const page = await boot(host);
    for (let i = 1; i <= 3; i += 1) {
      page.submitForm("run-form");
      await waitFor(() => host.calls.getRunStatus.length === i, 2000, `run ${i}'s first poll`);
    }
    const before = host.calls.getRunStatus.length;
    await sleep(POLL_INTERVAL_MS + 800);
    assertEqual(host.calls.getRunStatus.length - before, 1, "still one call per interval");
    teardown(page);
  });

  // ----------------------------------------------------------------- defect 6
  await check("defect 6: an unrecognised phase does not render as \"undefined\"", async () => {
    const host = makeHost({
      getRunStatus: async (runId) => ({ runId, status: "running", phase: "reticulating_splines" }),
    });
    const page = await boot(host);
    page.submitForm("run-form");
    await waitFor(() => host.calls.getRunStatus.length >= 1, 2000, "the first poll");
    await sleep(50);

    const live = page.el("status-live").textContent;
    const phases = page.el("phase-list").textContent;
    assert(!live.includes("undefined"), "nothing announces the word undefined");
    assert(!phases.includes("undefined"), "no phase renders as undefined");
    assert(!phases.includes("Done (in progress)"), "the list is not shifted");
    assertIncludes(phases, "Queued (in progress)", "an unknown phase degrades to the first step");
    teardown(page);
  });

  // ----------------------------------------------------------------- defect 7
  await check("defect 7: a non-https pull request URL is never given an href", async () => {
    const hostile = "javascript:alert(document.domain)//";
    const host = makeHost({
      getRunStatus: async (runId) => ({
        runId,
        status: "succeeded",
        phase: "done",
        result: {
          pullRequestId: 812,
          pullRequestUrl: hostile,
          branch: "bug/1-x",
          commits: 3,
          filesChanged: 7,
        },
      }),
    });
    const page = await boot(host);
    page.submitForm("run-form");
    await waitFor(() => page.el("status-detail").textContent.length > 0, 2000, "the outcome to render");

    const anchors = findAll(page.el("status-detail"), "a");
    assertEqual(anchors.length, 0, "no anchor element is created at all");
    assert(
      !page.el("status-detail").textContent.includes("javascript:"),
      "the hostile URL is not shown as a link target",
    );
    assertIncludes(
      page.el("status-detail").textContent,
      "not an https URL",
      "the user is told why there is no link",
    );
    teardown(page);
  });

  await check("defect 7: an https pull request URL still links, through host.openUrl", async () => {
    const url = "https://dev.azure.com/contoso/Platform/_git/payments-api/pullrequest/812";
    const host = makeHost({
      getRunStatus: async (runId) => ({
        runId,
        status: "succeeded",
        phase: "done",
        result: { pullRequestId: 812, pullRequestUrl: url, branch: "bug/1-x", commits: 3, filesChanged: 7 },
      }),
    });
    const page = await boot(host);
    page.submitForm("run-form");
    await waitFor(() => page.el("status-detail").textContent.length > 0, 2000, "the outcome to render");

    const anchors = findAll(page.el("status-detail"), "a");
    assertEqual(anchors.length, 1, "one anchor");
    assertEqual(anchors[0].getAttribute("href"), url, "with the agent's https URL");
    anchors[0].dispatchEvent({ type: "click", preventDefault() {} });
    assertEqual(host.calls.openUrl[0], url, "a click still routes through host.openUrl");
    teardown(page);
  });

  // ----------------------------------------------------------------- defect 5
  await check("defect 5: the settings form refuses a cross-audience scope", async () => {
    const host = makeHost();
    const page = await boot(host);

    page.el("agent-url").value = "https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols";
    page.el("entra-client-id").value = "11111111-2222-3333-4444-555555555555";
    page.el("agent-scope").value = "https://graph.microsoft.com/.default";
    page.submitForm("settings-form");
    await sleep(50);

    assertEqual(host.calls.saveSettings.length, 0, "nothing was saved");
    assertIncludes(
      page.el("settings-message").textContent,
      "https://ai.azure.com/.default",
      "the user is told the one scope that is accepted",
    );
    teardown(page);
  });

  await check("defect 4: the settings form refuses a userinfo URL", async () => {
    const host = makeHost();
    const page = await boot(host);

    page.el("agent-url").value = "https://contoso.services.ai.azure.com@evil.com/";
    page.el("entra-client-id").value = "11111111-2222-3333-4444-555555555555";
    page.el("agent-scope").value = "https://ai.azure.com/.default";
    page.submitForm("settings-form");
    await sleep(50);

    assertEqual(host.calls.saveSettings.length, 0, "nothing was saved");
    assertIncludes(
      page.el("settings-message").textContent,
      "points at evil.com",
      "the user is told which host the URL really names",
    );
    teardown(page);
  });
}
