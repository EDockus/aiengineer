/**
 * The published ticket-to-pr agent request/response contract, v1.
 *
 * Source of truth: docs/contract.md. This file is a
 * transcription of that contract and must not drift from it. In particular the
 * request body has exactly the fields below — the server rejects unknown fields
 * with 400, so there is deliberately no `harness`, `model` or `coder` field here.
 */

/** Branch names accepted by the contract. Enforced server-side; mirrored client-side. */
export const BRANCH_NAME_PATTERN = /^[A-Za-z0-9._/-]+$/;

/** POST {agent-application-url}/invocations */
export interface CreateRunRequest {
  version: "1";
  organization: string;
  project: string;
  workItem: { type: string; id: number };
  repository: string;
  branch: string;
  /** Optional. Defaults to the repository's default branch. */
  baseBranch?: string;
  /** Optional. Echoed back unchanged. */
  correlationId?: string;
}

/** Immediate 202 response. */
export interface CreateRunResponse {
  runId: string;
  status: RunStatusValue;
  correlationId?: string;
}

export type RunStatusValue = "queued" | "running" | "succeeded" | "failed";

/**
 * Run phases in the order the agent reports them. `queued` is a status rather
 * than a phase, but the UI shows it as the first step so the user sees progress
 * before the agent picks the run up.
 */
export type RunPhase =
  | "fetching_work_item"
  | "cloning"
  | "coding"
  | "pushing"
  | "creating_pr"
  | "done";

export const RUN_PHASES: readonly RunPhase[] = [
  "fetching_work_item",
  "cloning",
  "coding",
  "pushing",
  "creating_pr",
  "done",
];

/** Human-readable label for each phase, shown in the status panel. */
export const PHASE_LABELS: Record<RunPhase | "queued", string> = {
  queued: "Queued",
  fetching_work_item: "Reading the work item",
  cloning: "Cloning the repository",
  coding: "Writing the code",
  pushing: "Pushing the branch",
  creating_pr: "Creating the pull request",
  done: "Done",
};

/** GET /invocations/{runId} */
export interface RunStatus {
  runId: string;
  status: RunStatusValue;
  phase?: RunPhase;
  startedAt?: string;
  finishedAt?: string | null;
  result?: RunResult;
  error?: RunError;
  correlationId?: string;
}

export interface RunResult {
  pullRequestId: number;
  pullRequestUrl: string;
  branch: string;
  commits: number;
  filesChanged: number;
}

export interface RunError {
  code: string;
  message: string;
}

/** Exhaustive set of v1 error codes (docs/contract.md §6). Do not invent new ones. */
export const ERROR_CODES = [
  "work_item_not_found",
  "repository_not_found",
  "branch_exists",
  "ado_forbidden",
  "clone_failed",
  "harness_failed",
  "harness_no_changes",
  "push_failed",
  "pr_failed",
  "timeout",
  "internal",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

// ---------------------------------------------------------------- type guards
//
// Everything below validates a parsed JSON body from the agent before the UI is
// allowed to touch it. The agent is a trusted service, but "trusted" is not the
// same as "cannot be wrong": an older or newer build, a proxy that rewrites a
// body, or an error page served with a 200 would otherwise reach the DOM. An
// unrecognised `phase` used to render every step as "not started" and announce
// the literal word "undefined"; a missing `runId` used to produce a request to
// `GET /invocations/undefined`.

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

const RUN_STATUS_VALUES: readonly RunStatusValue[] = ["queued", "running", "succeeded", "failed"];

export function isRunStatusValue(value: unknown): value is RunStatusValue {
  return typeof value === "string" && (RUN_STATUS_VALUES as readonly string[]).includes(value);
}

export function isRunPhase(value: unknown): value is RunPhase {
  return typeof value === "string" && (RUN_PHASES as readonly string[]).includes(value);
}

/** Label for a phase, falling back to "Queued" rather than rendering `undefined`. */
export function phaseLabel(phase: RunPhase | "queued" | undefined): string {
  if (phase && Object.prototype.hasOwnProperty.call(PHASE_LABELS, phase)) {
    return PHASE_LABELS[phase];
  }
  return PHASE_LABELS.queued;
}

function isRunResult(value: unknown): value is RunResult {
  if (!isRecord(value)) return false;
  return (
    typeof value["pullRequestId"] === "number" &&
    isNonEmptyString(value["pullRequestUrl"]) &&
    isNonEmptyString(value["branch"]) &&
    typeof value["commits"] === "number" &&
    typeof value["filesChanged"] === "number"
  );
}

function isRunError(value: unknown): value is RunError {
  if (!isRecord(value)) return false;
  return isNonEmptyString(value["code"]) && typeof value["message"] === "string";
}

/** Validates the 202 body of POST /invocations. */
export function isCreateRunResponse(value: unknown): value is CreateRunResponse {
  if (!isRecord(value)) return false;
  if (!isNonEmptyString(value["runId"])) return false;
  if (!isRunStatusValue(value["status"])) return false;
  return isOptionalString(value["correlationId"]);
}

/** Validates the body of GET /invocations/{runId}. */
export function isRunStatus(value: unknown): value is RunStatus {
  if (!isRecord(value)) return false;
  if (!isNonEmptyString(value["runId"])) return false;
  if (!isRunStatusValue(value["status"])) return false;
  if (value["phase"] !== undefined && value["phase"] !== null && !isRunPhase(value["phase"])) {
    return false;
  }
  if (!isOptionalString(value["startedAt"]) || !isOptionalString(value["finishedAt"])) return false;
  if (!isOptionalString(value["correlationId"])) return false;
  if (value["result"] !== undefined && value["result"] !== null && !isRunResult(value["result"])) {
    return false;
  }
  if (value["error"] !== undefined && value["error"] !== null && !isRunError(value["error"])) {
    return false;
  }
  return true;
}
