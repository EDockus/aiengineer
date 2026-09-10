/**
 * Local development stub for HubHost.
 *
 * This substitutes for the Azure DevOps SDK, MSAL and the agent HTTP endpoint so
 * the hub can be exercised in a plain browser with no ADO organization. It is a
 * stub for the *seam only* — hub.ts, hub.html and styles.css are the real,
 * shipped files in both modes.
 */
import { ERROR_CODES, RUN_PHASES, type CreateRunRequest, type CreateRunResponse, type ErrorCode, type RunPhase, type RunStatus } from "../src/contract.js";
import { BRANCH_NAME_PATTERN } from "../src/contract.js";
import { validateSettings } from "../src/host.js";
import type {
  HubContext,
  HubHost,
  HubSettings,
  LoadedSettings,
  RepositorySummary,
  WorkItemSummary,
} from "../src/host.js";

export type Scenario = "success" | ErrorCode | "transport_error";

export interface HarnessOptions {
  scenario: Scenario;
  /** Milliseconds spent in each phase. */
  phaseMs: number;
  /** Simulated latency on every list/lookup call. */
  latencyMs: number;
  workItemMode: "normal" | "empty" | "error";
  repositoryMode: "normal" | "empty" | "error";
  /** Pre-populate settings so the form is usable immediately. */
  configured: boolean;
  /** Settings pinned by the extension package: shown, but not editable. */
  managed: boolean;
}

export const DEFAULT_OPTIONS: HarnessOptions = {
  scenario: "success",
  phaseMs: 1200,
  latencyMs: 250,
  workItemMode: "normal",
  repositoryMode: "normal",
  configured: true,
  managed: false,
};

export const SCENARIOS: { value: Scenario; label: string }[] = [
  { value: "success", label: "Succeeds (opens a PR)" },
  ...ERROR_CODES.map((code) => ({ value: code as Scenario, label: `Fails: ${code}` })),
  { value: "transport_error", label: "Agent unreachable (network error)" },
];

/*
 * Work item titles below deliberately include HTML and quote characters. If the
 * hub ever used innerHTML these would render as markup or fire; they must appear
 * verbatim as text in the picker. Keep them.
 */
const WORK_ITEMS: WorkItemSummary[] = [
  { id: 4123, type: "Bug", title: "Null reference when payment method is missing", state: "Active", source: "assigned" },
  { id: 4124, type: "User Story", title: 'Support "partial" refunds on the payments API', state: "New", source: "assigned" },
  { id: 4131, type: "Bug", title: "<img src=x onerror=\"alert('xss')\"> in customer note breaks the invoice view", state: "Active", source: "assigned" },
  { id: 4140, type: "Task", title: "Upgrade Ünicode & <b>bold</b> handling in the CSV export", state: "Active", source: "assigned" },
  { id: 3987, type: "Bug", title: "Retry loop spins when the ledger service returns 429", state: "Resolved", source: "recent" },
  { id: 3902, type: "Feature", title: "Idempotency keys for the charge endpoint", state: "Active", source: "recent" },
];

const EXTRA_WORK_ITEMS: WorkItemSummary[] = [
  { id: 9001, type: "Bug", title: "Looked up directly by ID, not in the default list", state: "New", source: "recent" },
];

const REPOSITORIES: RepositorySummary[] = [
  { id: "repo-1", name: "payments-api", defaultBranch: "refs/heads/main" },
  { id: "repo-2", name: "billing-worker", defaultBranch: "refs/heads/develop" },
  { id: "repo-3", name: "shared-contracts", defaultBranch: "refs/heads/main" },
];

const ERROR_MESSAGES: Record<ErrorCode, string> = {
  work_item_not_found: "Work item 4123 was not found in project Platform.",
  repository_not_found: "Repository 'payments-api' was not found in project Platform.",
  branch_exists: "refs/heads/bugfix/4123-null-ref already exists.",
  ado_forbidden: "TF401019: The Git repository does not exist or you do not have permission.",
  clone_failed: "fatal: could not read from remote repository (exit 128).",
  harness_failed:
    "codex exited with code 1\n  at src/payments/charge.py:88\n  TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'",
  harness_no_changes: "git status reported a clean working tree after the harness run.",
  push_failed: "! [remote rejected] bugfix/4123-null-ref -> bugfix/4123-null-ref (pre-receive hook declined)",
  pr_failed: "TF401398: The pull request cannot be created. Remote branch deleted.",
  timeout: "Harness exceeded T2PR_MAX_RUN_MINUTES=20 and was terminated.",
  internal: "Unhandled exception in run.py:214",
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface StubRun {
  runId: string;
  correlationId: string;
  request: CreateRunRequest;
  startedAt: number;
  phaseIndex: number;
  /** Phase at which the configured failure fires. */
  failAtIndex: number;
  scenario: Scenario;
  phaseMs: number;
}

/** Which phase each error code naturally fails at. */
const FAIL_PHASE: Record<ErrorCode, RunPhase> = {
  work_item_not_found: "fetching_work_item",
  repository_not_found: "fetching_work_item",
  branch_exists: "fetching_work_item",
  ado_forbidden: "fetching_work_item",
  clone_failed: "cloning",
  harness_failed: "coding",
  harness_no_changes: "coding",
  timeout: "coding",
  push_failed: "pushing",
  pr_failed: "creating_pr",
  internal: "coding",
};

export class StubHost implements HubHost {
  private settings: HubSettings = { agentUrl: "", entraClientId: "",
    entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", agentScope: "" };
  private run: StubRun | undefined;
  private log: (line: string) => void = () => {};

  constructor(private options: HarnessOptions) {
    if (options.configured) this.settings = configuredSettings();
  }

  setOptions(options: HarnessOptions): void {
    this.options = options;
  }

  onLog(sink: (line: string) => void): void {
    this.log = sink;
  }

  async initialize(): Promise<HubContext> {
    this.log("initialize()");
    await sleep(this.options.latencyMs);
    return { organization: "contoso", project: "Platform", userDisplayName: "Dev Harness User" };
  }

  notifyReady(): void {
    this.log("notifyReady()  [SDK.notifyLoadSucceeded + resize in production]");
  }

  async loadSettings(): Promise<LoadedSettings> {
    await sleep(this.options.latencyMs);
    const managed = this.options.managed;
    if (managed) this.settings = configuredSettings();
    this.log(
      `loadSettings() -> ${this.settings.agentUrl || "(not configured)"}${managed ? " [managed by the package]" : ""}`,
    );
    return { settings: { ...this.settings }, managed };
  }

  async saveSettings(settings: HubSettings): Promise<void> {
    await sleep(this.options.latencyMs);
    const check = validateSettings(settings);
    if (!check.valid) {
      // The real host refuses to store settings it would not accept back.
      this.log(`saveSettings() -> rejected: ${check.message ?? ""}`);
      throw new Error(check.message ?? "invalid settings");
    }
    this.settings = { ...settings };
    this.log(`saveSettings() -> ${settings.agentUrl}`);
  }

  async listMyWorkItems(): Promise<WorkItemSummary[]> {
    await sleep(this.options.latencyMs);
    if (this.options.workItemMode === "error") {
      this.log("listMyWorkItems() -> throwing (simulated ADO failure)");
      throw new Error("simulated Azure DevOps failure");
    }
    if (this.options.workItemMode === "empty") {
      this.log("listMyWorkItems() -> []");
      return [];
    }
    this.log(`listMyWorkItems() -> ${WORK_ITEMS.length} items`);
    return WORK_ITEMS.map((item) => ({ ...item }));
  }

  async findWorkItemById(id: number): Promise<WorkItemSummary | undefined> {
    await sleep(this.options.latencyMs);
    const found = [...WORK_ITEMS, ...EXTRA_WORK_ITEMS].find((item) => item.id === id);
    this.log(`findWorkItemById(${id}) -> ${found ? "found" : "not found"}`);
    return found ? { ...found } : undefined;
  }

  async listRepositories(): Promise<RepositorySummary[]> {
    await sleep(this.options.latencyMs);
    if (this.options.repositoryMode === "error") {
      this.log("listRepositories() -> throwing (simulated ADO failure)");
      throw new Error("simulated Azure DevOps failure");
    }
    if (this.options.repositoryMode === "empty") {
      this.log("listRepositories() -> []");
      return [];
    }
    this.log(`listRepositories() -> ${REPOSITORIES.length} repositories`);
    return REPOSITORIES.map((repo) => ({ ...repo }));
  }

  async submitRun(request: CreateRunRequest): Promise<CreateRunResponse> {
    this.log(`POST /invocations ${JSON.stringify(request)}`);

    // Assert the contract the real server enforces, so a regression that adds a
    // field or a bad branch name fails here rather than silently in production.
    assertContract(request);

    await sleep(this.options.latencyMs);

    if (this.options.scenario === "transport_error") {
      this.log("POST /invocations -> network error (simulated)");
      throw new Error(
        "Could not reach the agent. Check the Agent Application URL in settings and that the service is running.",
      );
    }

    const scenario = this.options.scenario;
    const failPhase = scenario === "success" ? undefined : FAIL_PHASE[scenario as ErrorCode];
    this.run = {
      runId: `dev-${Math.random().toString(36).slice(2, 10)}`,
      correlationId: request.correlationId ?? "",
      request,
      startedAt: Date.now(),
      phaseIndex: -1, // -1 == queued
      failAtIndex: failPhase ? RUN_PHASES.indexOf(failPhase) : -1,
      scenario,
      phaseMs: this.options.phaseMs,
    };
    this.log(`202 Accepted runId=${this.run.runId}`);
    return { runId: this.run.runId, status: "queued", correlationId: this.run.correlationId };
  }

  async getRunStatus(runId: string): Promise<RunStatus> {
    await sleep(Math.min(this.options.latencyMs, 120));
    const run = this.run;
    if (!run || run.runId !== runId) {
      throw new Error(`No such run: ${runId}`);
    }

    // Advance the run by wall-clock time so it progresses on the real 5 s poll.
    const elapsed = Date.now() - run.startedAt;
    const index = Math.min(Math.floor(elapsed / run.phaseMs) - 1, RUN_PHASES.length - 1);
    run.phaseIndex = index;

    const startedAt = new Date(run.startedAt).toISOString();

    if (index < 0) {
      this.log(`GET /invocations/${runId} -> queued`);
      return { runId, status: "queued", startedAt, correlationId: run.correlationId };
    }

    if (run.failAtIndex >= 0 && index >= run.failAtIndex) {
      const code = run.scenario as ErrorCode;
      const phase = RUN_PHASES[run.failAtIndex]!;
      this.log(`GET /invocations/${runId} -> failed ${code} at ${phase}`);
      return {
        runId,
        status: "failed",
        phase,
        startedAt,
        finishedAt: new Date().toISOString(),
        error: { code, message: ERROR_MESSAGES[code] },
        correlationId: run.correlationId,
      };
    }

    const phase = RUN_PHASES[index]!;
    if (phase !== "done") {
      this.log(`GET /invocations/${runId} -> running ${phase}`);
      return { runId, status: "running", phase, startedAt, correlationId: run.correlationId };
    }

    this.log(`GET /invocations/${runId} -> succeeded`);
    return {
      runId,
      status: "succeeded",
      phase: "done",
      startedAt,
      finishedAt: new Date().toISOString(),
      result: {
        pullRequestId: 812,
        pullRequestUrl: `https://dev.azure.com/contoso/Platform/_git/${run.request.repository}/pullrequest/812`,
        branch: run.request.branch,
        commits: 3,
        filesChanged: 7,
      },
      correlationId: run.correlationId,
    };
  }

  openUrl(url: string): void {
    this.log(`openUrl(${url})  [host navigation service in production]`);
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

/** The exact fields the contract allows on a request body (docs/contract.md §3). */
const ALLOWED_FIELDS = new Set([
  "version",
  "organization",
  "project",
  "workItem",
  "repository",
  "branch",
  "baseBranch",
  "correlationId",
]);

function assertContract(request: CreateRunRequest): void {
  const unknown = Object.keys(request).filter((key) => !ALLOWED_FIELDS.has(key));
  if (unknown.length > 0) {
    // Mirrors the real server, which rejects unknown fields with 400.
    throw new Error(`Contract violation: unknown field(s) ${unknown.join(", ")} in the request body`);
  }
  if (request.version !== "1") throw new Error('Contract violation: version must be "1"');
  if (!Number.isInteger(request.workItem?.id) || request.workItem.id <= 0) {
    throw new Error("Contract violation: workItem.id must be a positive integer");
  }
  if (!BRANCH_NAME_PATTERN.test(request.branch)) {
    throw new Error(`Contract violation: branch "${request.branch}" does not match ^[A-Za-z0-9._/-]+$`);
  }
}

export function configuredSettings(): HubSettings {
  return {
    agentUrl: "https://dev-harness.services.ai.azure.com/api/projects/harness/agents/ticket-to-pr/endpoint/protocols",
    entraClientId: "11111111-2222-3333-4444-555555555555",
    entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    agentScope: "https://ai.azure.com/.default",
  };
}
