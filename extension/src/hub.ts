import {
  RUN_PHASES,
  phaseLabel,
  type CreateRunRequest,
  type RunPhase,
  type RunStatus,
} from "./contract.js";
import { describeError, needsNewBranchName } from "./errors.js";
import { suggestBranchName, validateBranchName } from "./branch.js";
import {
  EMPTY_SETTINGS,
  FOUNDRY_AGENT_SCOPE,
  isConfigured,
  isRetryableError,
  isSafeHttpsUrl,
  shortBranchName,
  validateAgentScope,
  validateAgentUrl,
  validateEntraClientId,
  type HubContext,
  type HubHost,
  type HubSettings,
  type RepositorySummary,
  type WorkItemSummary,
} from "./host.js";
import { clear, correlationId, el, requireEl, setText, show } from "./dom.js";

/** docs/contract.md §8: poll status every 5 s. */
const POLL_INTERVAL_MS = 5000;
/** Give up polling well past the agent's own hard timeout rather than forever. */
const POLL_CEILING_MS = 45 * 60 * 1000;
/**
 * Consecutive failed polls tolerated before the run is reported as lost. The
 * wall-clock ceiling alone is not a budget: at one request every five seconds it
 * allows ~540 requests, which is the wrong answer to an agent that is down.
 */
const MAX_POLL_FAILURES = 5;

type LoadState<T> =
  | { kind: "loading" }
  | { kind: "ready"; items: T[] }
  | { kind: "error"; message: string };

interface State {
  context: HubContext;
  settings: HubSettings;
  /** True when settings are pinned by the extension package and cannot be edited here. */
  managedSettings: boolean;
  workItems: LoadState<WorkItemSummary>;
  repositories: LoadState<RepositorySummary>;
  selectedWorkItem?: WorkItemSummary;
  /** Set once the user edits the branch field, so we stop overwriting it. */
  branchTouched: boolean;
  submitting: boolean;
  run?: { runId: string; correlationId: string; startedAt: number };
}

export async function bootstrap(host: HubHost): Promise<void> {
  const state: State = {
    context: { organization: "", project: "", userDisplayName: "" },
    settings: { ...EMPTY_SETTINGS },
    managedSettings: false,
    workItems: { kind: "loading" },
    repositories: { kind: "loading" },
    branchTouched: false,
    submitting: false,
  };

  // --------------------------------------------------------------- elements

  const contextLine = requireEl<HTMLParagraphElement>("#context-line");
  const configBanner = requireEl<HTMLDivElement>("#config-banner");
  const settingsDetails = requireEl<HTMLDetailsElement>("#settings-details");
  const settingsForm = requireEl<HTMLFormElement>("#settings-form");
  const agentUrlInput = requireEl<HTMLInputElement>("#agent-url");
  const clientIdInput = requireEl<HTMLInputElement>("#entra-client-id");
  const tenantIdInput = requireEl<HTMLInputElement>("#entra-tenant-id");
  const agentScopeInput = requireEl<HTMLInputElement>("#agent-scope");
  const settingsMessage = requireEl<HTMLParagraphElement>("#settings-message");
  const settingsManaged = requireEl<HTMLParagraphElement>("#settings-managed");
  const saveSettingsButton = requireEl<HTMLButtonElement>("#save-settings");

  const runForm = requireEl<HTMLFormElement>("#run-form");
  const workItemSearch = requireEl<HTMLInputElement>("#work-item-search");
  const workItemSelect = requireEl<HTMLSelectElement>("#work-item");
  const workItemMessage = requireEl<HTMLParagraphElement>("#work-item-message");
  const workItemRetry = requireEl<HTMLButtonElement>("#work-item-retry");
  const repositorySelect = requireEl<HTMLSelectElement>("#repository");
  const repositoryMessage = requireEl<HTMLParagraphElement>("#repository-message");
  const repositoryRetry = requireEl<HTMLButtonElement>("#repository-retry");
  const baseBranchInput = requireEl<HTMLInputElement>("#base-branch");
  const branchInput = requireEl<HTMLInputElement>("#branch");
  const branchMessage = requireEl<HTMLParagraphElement>("#branch-message");
  const submitButton = requireEl<HTMLButtonElement>("#submit");
  const formMessage = requireEl<HTMLParagraphElement>("#form-message");

  const statusPanel = requireEl<HTMLElement>("#status-panel");
  const statusLive = requireEl<HTMLParagraphElement>("#status-live");
  const phaseList = requireEl<HTMLOListElement>("#phase-list");
  const statusDetail = requireEl<HTMLDivElement>("#status-detail");
  const runMeta = requireEl<HTMLParagraphElement>("#run-meta");

  // ------------------------------------------------------------- data loads

  async function loadWorkItems(): Promise<void> {
    state.workItems = { kind: "loading" };
    renderWorkItems();
    try {
      const items = await host.listMyWorkItems();
      state.workItems = { kind: "ready", items };
    } catch {
      state.workItems = {
        kind: "error",
        message: "Could not load your work items from Azure DevOps.",
      };
    }
    renderWorkItems();
  }

  async function loadRepositories(): Promise<void> {
    state.repositories = { kind: "loading" };
    renderRepositories();
    try {
      const items = await host.listRepositories();
      state.repositories = { kind: "ready", items };
    } catch {
      state.repositories = {
        kind: "error",
        message: "Could not load the repositories in this project.",
      };
    }
    renderRepositories();
  }

  // ------------------------------------------------------------- work items

  function matchingWorkItems(): WorkItemSummary[] {
    if (state.workItems.kind !== "ready") return [];
    const query = workItemSearch.value.trim().toLowerCase();
    if (!query) return state.workItems.items;
    return state.workItems.items.filter(
      (item) =>
        String(item.id).includes(query) ||
        item.title.toLowerCase().includes(query) ||
        item.type.toLowerCase().includes(query),
    );
  }

  function optionLabel(item: WorkItemSummary): string {
    const state_ = item.state ? ` · ${item.state}` : "";
    return `#${item.id} · ${item.type} · ${item.title}${state_}`;
  }

  function renderWorkItems(): void {
    clear(workItemSelect);
    show(workItemRetry, state.workItems.kind === "error");

    if (state.workItems.kind === "loading") {
      workItemSelect.disabled = true;
      workItemSelect.append(el("option", { value: "" }, ["Loading your work items…"]));
      setText(workItemMessage, "Loading your work items…");
      show(workItemMessage, true);
      workItemMessage.dataset["tone"] = "muted";
      updateSubmitState();
      return;
    }

    if (state.workItems.kind === "error") {
      workItemSelect.disabled = true;
      workItemSelect.append(el("option", { value: "" }, ["Unavailable"]));
      setText(
        workItemMessage,
        `${state.workItems.message} You can still type an ID in the search box above to use a specific work item.`,
      );
      show(workItemMessage, true);
      workItemMessage.dataset["tone"] = "error";
      updateSubmitState();
      return;
    }

    const matches = matchingWorkItems();
    workItemSelect.disabled = matches.length === 0;

    if (matches.length === 0) {
      const empty = state.workItems.items.length === 0;
      workItemSelect.append(el("option", { value: "" }, [empty ? "Nothing assigned to you" : "No matches"]));
      setText(
        workItemMessage,
        empty
          ? "You have no open work items assigned to you in this project, and nothing recent. Type a work item ID above to use one anyway."
          : "No work item matches that search. Type a full ID to look one up directly.",
      );
      show(workItemMessage, true);
      workItemMessage.dataset["tone"] = "muted";
      state.selectedWorkItem = undefined;
      updateSubmitState();
      return;
    }

    // Text only — a work item title is user-supplied and may contain markup.
    for (const item of matches) {
      workItemSelect.append(el("option", { value: String(item.id) }, [optionLabel(item)]));
    }

    const stillSelected =
      state.selectedWorkItem && matches.some((m) => m.id === state.selectedWorkItem?.id);
    state.selectedWorkItem = stillSelected ? state.selectedWorkItem : matches[0];
    workItemSelect.value = String(state.selectedWorkItem?.id ?? "");

    const assigned = matches.filter((m) => m.source === "assigned").length;
    setText(
      workItemMessage,
      `${matches.length} work ${matches.length === 1 ? "item" : "items"} (${assigned} assigned to you, ${matches.length - assigned} recently viewed).`,
    );
    show(workItemMessage, true);
    workItemMessage.dataset["tone"] = "muted";

    applySuggestedBranch();
    updateSubmitState();
  }

  /** Look up a work item by id when the search box holds a bare number. */
  async function lookupTypedId(): Promise<void> {
    const query = workItemSearch.value.trim();
    if (!/^\d+$/.test(query)) return;
    const id = Number(query);
    if (state.workItems.kind === "ready" && state.workItems.items.some((i) => i.id === id)) return;

    const found = await host.findWorkItemById(id);
    if (!found) {
      setText(
        workItemMessage,
        `No work item #${id} is visible to you in this project.`,
      );
      workItemMessage.dataset["tone"] = "error";
      show(workItemMessage, true);
      return;
    }
    const existing = state.workItems.kind === "ready" ? state.workItems.items : [];
    state.workItems = { kind: "ready", items: [found, ...existing.filter((i) => i.id !== found.id)] };
    state.selectedWorkItem = found;
    renderWorkItems();
  }

  // ----------------------------------------------------------- repositories

  function selectedRepository(): RepositorySummary | undefined {
    if (state.repositories.kind !== "ready") return undefined;
    return state.repositories.items.find((r) => r.id === repositorySelect.value);
  }

  function renderRepositories(): void {
    clear(repositorySelect);
    show(repositoryRetry, state.repositories.kind === "error");

    if (state.repositories.kind === "loading") {
      repositorySelect.disabled = true;
      repositorySelect.append(el("option", { value: "" }, ["Loading repositories…"]));
      setText(repositoryMessage, "Loading repositories…");
      repositoryMessage.dataset["tone"] = "muted";
      show(repositoryMessage, true);
      updateSubmitState();
      return;
    }

    if (state.repositories.kind === "error") {
      repositorySelect.disabled = true;
      repositorySelect.append(el("option", { value: "" }, ["Unavailable"]));
      setText(repositoryMessage, state.repositories.message);
      repositoryMessage.dataset["tone"] = "error";
      show(repositoryMessage, true);
      updateSubmitState();
      return;
    }

    if (state.repositories.items.length === 0) {
      repositorySelect.disabled = true;
      repositorySelect.append(el("option", { value: "" }, ["No repositories"]));
      setText(
        repositoryMessage,
        "This project has no Git repositories the agent can use.",
      );
      repositoryMessage.dataset["tone"] = "muted";
      show(repositoryMessage, true);
      updateSubmitState();
      return;
    }

    repositorySelect.disabled = false;
    for (const repo of state.repositories.items) {
      repositorySelect.append(el("option", { value: repo.id }, [repo.name]));
    }
    repositorySelect.value = state.repositories.items[0]?.id ?? "";
    renderBaseBranchHint();
    show(repositoryMessage, false);
    updateSubmitState();
  }

  function renderBaseBranchHint(): void {
    const repo = selectedRepository();
    const branch = shortBranchName(repo?.defaultBranch);
    baseBranchInput.placeholder = branch
      ? `${branch} (repository default)`
      : "repository default";
  }

  // ----------------------------------------------------------------- branch

  function applySuggestedBranch(): void {
    if (state.branchTouched) return;
    const item = state.selectedWorkItem;
    branchInput.value = item ? suggestBranchName(item) : "";
    validateBranchField();
  }

  function validateBranchField(): boolean {
    const result = validateBranchName(branchInput.value);
    if (result.valid) {
      branchInput.removeAttribute("aria-invalid");
      show(branchMessage, false);
      setText(branchMessage, "");
      return true;
    }
    branchInput.setAttribute("aria-invalid", "true");
    setText(branchMessage, result.message ?? "That branch name is not valid.");
    branchMessage.dataset["tone"] = "error";
    show(branchMessage, true);
    return false;
  }

  // --------------------------------------------------------------- settings

  function renderSettings(): void {
    agentUrlInput.value = state.settings.agentUrl;
    clientIdInput.value = state.settings.entraClientId;
    tenantIdInput.value = state.settings.entraTenantId;
    // The scope is a constant, not a choice: prefill it so an unconfigured
    // form is one field shorter and nobody has to guess the literal.
    agentScopeInput.value = state.settings.agentScope || FOUNDRY_AGENT_SCOPE;

    // Pinned by the package: show the values, but nothing here can change them.
    for (const input of [agentUrlInput, clientIdInput, tenantIdInput, agentScopeInput]) {
      input.readOnly = state.managedSettings;
      if (state.managedSettings) input.setAttribute("aria-readonly", "true");
      else input.removeAttribute("aria-readonly");
    }
    saveSettingsButton.disabled = state.managedSettings;
    show(saveSettingsButton, !state.managedSettings);
    show(settingsManaged, state.managedSettings);

    const configured = isConfigured(state.settings);
    show(configBanner, !configured);
    if (!configured) {
      settingsDetails.open = true;
    }
    updateSubmitState();
  }

  async function saveSettings(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (state.managedSettings) {
      return settingsFeedback(
        "These settings are set by the installed extension package and cannot be changed here.",
        "error",
      );
    }
    const candidate: HubSettings = {
      agentUrl: agentUrlInput.value.trim().replace(/\/+$/, ""),
      entraClientId: clientIdInput.value.trim(),
      entraTenantId: tenantIdInput.value.trim(),
      agentScope: agentScopeInput.value.trim(),
    };

    const urlCheck = validateAgentUrl(candidate.agentUrl);
    if (!urlCheck.valid) {
      agentUrlInput.focus();
      return settingsFeedback(urlCheck.message ?? "Check the Agent Application URL.", "error");
    }
    const clientIdCheck = validateEntraClientId(candidate.entraClientId);
    if (!clientIdCheck.valid) {
      clientIdInput.focus();
      return settingsFeedback(clientIdCheck.message ?? "Check the Entra client ID.", "error");
    }
    const scopeCheck = validateAgentScope(candidate.agentScope);
    if (!scopeCheck.valid) {
      agentScopeInput.focus();
      return settingsFeedback(scopeCheck.message ?? "Check the agent scope.", "error");
    }

    settingsFeedback("Saving…", "muted");
    try {
      await host.saveSettings(candidate);
      state.settings = candidate;
      renderSettings();
      settingsFeedback("Settings saved for you, in this project.", "success");
    } catch {
      settingsFeedback(
        "Could not save the settings. Azure DevOps refused to store the settings document for your account.",
        "error",
      );
    }
  }

  function settingsFeedback(message: string, tone: "muted" | "error" | "success"): void {
    setText(settingsMessage, message);
    settingsMessage.dataset["tone"] = tone;
    show(settingsMessage, true);
  }

  // ------------------------------------------------------------ status panel

  function announce(message: string): void {
    setText(statusLive, message);
  }

  function renderPhases(current: RunPhase | "queued" | undefined, failed: boolean): void {
    clear(phaseList);
    const order: (RunPhase | "queued")[] = ["queued", ...RUN_PHASES];
    // A phase the extension does not know would give -1, which renders every
    // step as "not started". Unknown phases are rejected at the host boundary;
    // clamping here means a future one degrades to "queued" instead of lying.
    const currentIndex = current ? Math.max(order.indexOf(current), 0) : 0;

    for (const [index, phase] of order.entries()) {
      let status: "done" | "current" | "pending" | "failed" = "pending";
      if (index < currentIndex) status = "done";
      else if (index === currentIndex) status = failed ? "failed" : "current";
      if (!failed && current === "done") status = "done";

      const marker = el("span", { class: "phase-marker", "aria-hidden": "true" }, [
        status === "done" ? "✓" : status === "failed" ? "✕" : status === "current" ? "•" : "",
      ]);
      const label = el("span", { class: "phase-label" }, [phaseLabel(phase)]);
      const srHint =
        status === "done"
          ? " (complete)"
          : status === "current"
            ? " (in progress)"
            : status === "failed"
              ? " (failed)"
              : " (not started)";
      phaseList.append(
        el("li", { class: "phase", "data-status": status }, [
          marker,
          label,
          el("span", { class: "sr-only" }, [srHint]),
        ]),
      );
    }
  }

  function renderRunning(status: RunStatus): void {
    const phase = status.phase ?? "queued";
    renderPhases(status.status === "queued" ? "queued" : phase, false);
    clear(statusDetail);
    announce(`${phaseLabel(phase)}. The agent is still working; this can take several minutes.`);
  }

  function renderSucceeded(status: RunStatus): void {
    renderPhases("done", false);
    clear(statusDetail);

    const result = status.result;
    if (!result) {
      announce("The run finished, but the agent did not return a pull request link.");
      statusDetail.append(
        el("p", { class: "message", "data-tone": "muted" }, [
          "The run finished, but the agent did not return a pull request link.",
        ]),
      );
      return;
    }

    announce(`Done. Pull request ${result.pullRequestId} is open on branch ${result.branch}.`);

    // The URL comes off the wire. The click handler routes through host.openUrl,
    // but an `href` is also a middle-click, a "copy link address" and a
    // context-menu "open in new tab" — none of which run the handler. So the
    // scheme is checked before the anchor exists, not on the way out of it:
    // anything that is not plain https never becomes a link.
    const linkable = isSafeHttpsUrl(result.pullRequestUrl);

    const details = el("dl", { class: "result-grid" }, [
      el("dt", {}, ["Branch"]),
      el("dd", {}, [result.branch]),
      el("dt", {}, ["Commits"]),
      el("dd", {}, [String(result.commits)]),
      el("dt", {}, ["Files changed"]),
      el("dd", {}, [String(result.filesChanged)]),
    ]);

    const outcome = el("div", { class: "outcome", "data-tone": "success" }, [
      el("p", { class: "outcome-headline" }, ["The agent opened a pull request."]),
      details,
    ]);

    if (linkable) {
      const link = el(
        "a",
        { class: "pr-link", href: result.pullRequestUrl, rel: "noopener noreferrer", target: "_blank" },
        [`Open pull request #${result.pullRequestId}`],
      );
      link.addEventListener("click", (event) => {
        event.preventDefault();
        host.openUrl(result.pullRequestUrl);
      });
      outcome.append(link);
    } else {
      outcome.append(
        el("p", { class: "message", "data-tone": "error" }, [
          `The agent returned pull request ${result.pullRequestId}, but the link it gave is not an https URL, so it is not shown as a link. Open the pull request from the repository instead.`,
        ]),
      );
    }

    statusDetail.append(outcome);
  }

  function renderFailed(status: RunStatus): void {
    renderPhases(status.phase ?? "queued", true);
    clear(statusDetail);

    const sentence = describeError(status.error);
    announce(`The run failed. ${sentence}`);

    const block = el("div", { class: "outcome", "data-tone": "error" }, [
      el("p", { class: "outcome-headline" }, ["The run did not produce a pull request."]),
      el("p", { class: "outcome-body" }, [sentence]),
    ]);

    // The agent's own message is untrusted text; it is appended as a text node.
    const detail = status.error?.message?.trim();
    if (detail) {
      block.append(
        el("details", { class: "error-detail" }, [
          el("summary", {}, ["Detail from the agent"]),
          el("pre", { class: "error-text" }, [detail]),
        ]),
      );
    }
    if (status.error?.code) {
      block.append(el("p", { class: "error-code" }, [`Error code: ${status.error.code}`]));
    }
    if (needsNewBranchName(status.error)) {
      block.append(
        el("p", { class: "outcome-body" }, ["Change the branch name above, then submit again."]),
      );
      branchInput.focus();
      branchInput.select();
    }
    statusDetail.append(block);
  }

  function renderTransportFailure(message: string): void {
    show(statusPanel, true);
    renderPhases("queued", true);
    clear(statusDetail);
    announce(message);
    statusDetail.append(
      el("div", { class: "outcome", "data-tone": "error" }, [
        el("p", { class: "outcome-headline" }, ["The run could not be started."]),
        el("p", { class: "outcome-body" }, [message]),
      ]),
    );
  }

  function renderRunMeta(): void {
    if (!state.run) {
      show(runMeta, false);
      return;
    }
    setText(runMeta, `Run ID ${state.run.runId} · correlation ID ${state.run.correlationId}`);
    show(runMeta, true);
  }

  // ------------------------------------------------------------ run + poll

  function buildRequest(): CreateRunRequest | undefined {
    const workItem = state.selectedWorkItem;
    const repository = selectedRepository();
    if (!workItem || !repository) return undefined;

    // Exactly the contract fields. No harness/model/coder — the server rejects
    // unknown fields with 400.
    const request: CreateRunRequest = {
      version: "1",
      organization: state.context.organization,
      project: state.context.project,
      workItem: { type: workItem.type, id: workItem.id },
      repository: repository.name,
      branch: branchInput.value.trim(),
      correlationId: correlationId(),
    };
    const baseBranch = baseBranchInput.value.trim();
    if (baseBranch) request.baseBranch = baseBranch;
    return request;
  }

  function updateSubmitState(): void {
    const ready =
      !state.submitting &&
      isConfigured(state.settings) &&
      Boolean(state.selectedWorkItem) &&
      Boolean(selectedRepository()) &&
      validateBranchName(branchInput.value).valid;
    submitButton.disabled = !ready;
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    show(formMessage, false);

    if (!validateBranchField()) {
      branchInput.focus();
      return;
    }
    const request = buildRequest();
    if (!request) {
      setText(formMessage, "Pick a work item and a repository first.");
      formMessage.dataset["tone"] = "error";
      show(formMessage, true);
      return;
    }

    // A previous run's poll chain would otherwise keep its timer. `poll` re-reads
    // `state.run` on every tick, so an orphaned chain does not stop at the run-id
    // guard — it picks up the *new* run and polls it alongside the new chain,
    // doubling the request rate with every submit.
    stopPolling();

    state.submitting = true;
    state.run = undefined;
    updateSubmitState();
    submitButton.dataset["busy"] = "true";
    setText(submitButton, "Submitting…");
    show(statusPanel, true);
    renderPhases("queued", false);
    clear(statusDetail);
    show(runMeta, false);
    announce("Submitting the run to the agent…");

    try {
      const accepted = await host.submitRun(request);
      state.run = {
        runId: accepted.runId,
        correlationId: accepted.correlationId ?? request.correlationId ?? "",
        startedAt: Date.now(),
      };
      renderRunMeta();
      announce("The agent accepted the run. Waiting for it to start…");
      poll();
    } catch (error) {
      finishSubmitting();
      renderTransportFailure(
        error instanceof Error && error.message
          ? error.message
          : "Could not reach the agent. Check the Agent Application URL in settings.",
      );
    }
  }

  function finishSubmitting(): void {
    // Nothing is following the run any more, so nothing should be scheduled to.
    stopPolling();
    state.submitting = false;
    delete submitButton.dataset["busy"];
    setText(submitButton, "Create PR with agent");
    updateSubmitState();
  }

  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let pollFailures = 0;

  function stopPolling(): void {
    clearTimeout(pollTimer);
    pollTimer = undefined;
    pollFailures = 0;
  }

  /**
   * One poll failed. Decides between another attempt and giving up, and returns
   * the sentence to show when giving up.
   *
   * A permanent failure — 401, 403, a 404 for a run id the agent does not know,
   * a body this extension cannot parse, a sign-in that needs a user gesture — is
   * permanent on the next attempt too. Retrying one of those on a five-second
   * timer until the 45-minute ceiling is ~540 requests that cannot succeed, and
   * leaves the user watching a spinner instead of reading the reason.
   */
  function pollFailureOutcome(error: unknown, run: { startedAt: number }): string | undefined {
    const message = error instanceof Error && error.message ? error.message : "";
    if (!isRetryableError(error)) {
      return message || "The agent refused the request for this run's status.";
    }
    if (pollFailures >= MAX_POLL_FAILURES) {
      return `Lost contact with the agent after ${MAX_POLL_FAILURES} attempts in a row. The run may still be going — check the repository for a new pull request.${message ? ` Last error: ${message}` : ""}`;
    }
    if (Date.now() - run.startedAt >= POLL_CEILING_MS) {
      return "Lost contact with the agent while waiting for this run.";
    }
    return undefined; // retry
  }

  function poll(): void {
    const run = state.run;
    if (!run) return;

    void (async () => {
      let status: RunStatus;
      try {
        status = await host.getRunStatus(run.runId);
      } catch (error: unknown) {
        if (state.run?.runId !== run.runId) return; // superseded by a newer run
        pollFailures += 1;
        const giveUp = pollFailureOutcome(error, run);
        if (giveUp) {
          finishSubmitting();
          renderTransportFailure(giveUp);
          return;
        }
        pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
        return;
      }
      if (state.run?.runId !== run.runId) return; // superseded by a newer run
      pollFailures = 0;

      if (status.status === "succeeded") {
        finishSubmitting();
        renderSucceeded(status);
        return;
      }
      if (status.status === "failed") {
        finishSubmitting();
        renderFailed(status);
        return;
      }

      renderRunning(status);
      if (Date.now() - run.startedAt >= POLL_CEILING_MS) {
        finishSubmitting();
        renderTransportFailure(
          "Stopped waiting for this run after 45 minutes. It may still be running — check the repository for a new pull request.",
        );
        return;
      }
      pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
    })();
  }

  // ------------------------------------------------------------------ wiring

  settingsForm.addEventListener("submit", (event) => void saveSettings(event));

  workItemSearch.addEventListener("input", () => renderWorkItems());
  let lookupTimer: ReturnType<typeof setTimeout> | undefined;
  workItemSearch.addEventListener("input", () => {
    clearTimeout(lookupTimer);
    lookupTimer = setTimeout(() => void lookupTypedId(), 400);
  });
  workItemSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(lookupTimer);
      void lookupTypedId();
    }
  });

  workItemSelect.addEventListener("change", () => {
    const id = Number(workItemSelect.value);
    state.selectedWorkItem = matchingWorkItems().find((item) => item.id === id);
    applySuggestedBranch();
    updateSubmitState();
  });

  workItemRetry.addEventListener("click", () => void loadWorkItems());
  repositoryRetry.addEventListener("click", () => void loadRepositories());
  repositorySelect.addEventListener("change", () => {
    renderBaseBranchHint();
    updateSubmitState();
  });

  branchInput.addEventListener("input", () => {
    state.branchTouched = true;
    validateBranchField();
    updateSubmitState();
  });
  branchInput.addEventListener("blur", () => validateBranchField());

  runForm.addEventListener("submit", (event) => void submit(event));

  window.addEventListener("beforeunload", () => clearTimeout(pollTimer));

  // -------------------------------------------------------------- start-up

  state.context = await host.initialize();
  setText(
    contextLine,
    `${state.context.organization} / ${state.context.project}` +
      (state.context.userDisplayName ? ` · signed in as ${state.context.userDisplayName}` : ""),
  );

  const loaded = await host.loadSettings();
  state.settings = loaded.settings;
  state.managedSettings = loaded.managed;
  renderSettings();

  show(statusPanel, false);
  renderPhases("queued", false);

  await Promise.all([loadWorkItems(), loadRepositories()]);

  host.notifyReady();
}
