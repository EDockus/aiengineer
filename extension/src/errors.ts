import { ERROR_CODES, type ErrorCode, type RunError } from "./contract.js";

/**
 * Every contract error code rendered as a sentence a human can act on.
 * The raw code is never the primary thing shown to the user; it is surfaced
 * only as secondary detail so it can be quoted in a support request.
 */
const SENTENCES: Record<ErrorCode, string> = {
  work_item_not_found:
    "The agent could not find that work item. Check the ID is right and that the agent identity has permission to view work items in this project.",
  repository_not_found:
    "The agent could not find that repository in this project. Pick a different repository, or ask an administrator to grant the agent identity access to it.",
  branch_exists:
    "A branch with that name already exists in the repository. Choose a different branch name and submit again.",
  ado_forbidden:
    "Azure DevOps refused the agent's request. The agent identity needs Basic access in this organization, plus Read, Contribute, Create branch and Contribute to pull requests on this repository.",
  clone_failed:
    "The agent could not clone the repository. Check that the base branch exists and that the agent identity has Read access to the repository.",
  harness_failed:
    "The coding run did not finish successfully, so nothing was pushed and no branch was left behind. The detail below is from the coding harness.",
  harness_no_changes:
    "The coding run finished without changing any files, so there was nothing to raise a pull request for. Adding acceptance criteria or repro steps to the work item usually helps.",
  push_failed:
    "The agent wrote the code but could not push the branch. Check that the agent identity has Contribute and Create branch permission on this repository, and that no branch policy blocks it.",
  pr_failed:
    "The agent pushed the branch but could not open the pull request, so it removed the branch again. Check that the agent identity has Contribute to pull requests on this repository.",
  timeout:
    "The coding run ran past its time limit and was stopped, so nothing was pushed. A smaller or more specific work item usually completes in time.",
  internal:
    "The agent hit an unexpected error. Nothing was pushed. Try again — if it keeps happening, quote the run ID below when you report it.",
};

const KNOWN = new Set<string>(ERROR_CODES);

export function isKnownErrorCode(code: string): code is ErrorCode {
  return KNOWN.has(code);
}

/**
 * Turns a contract error into a sentence. Unknown codes (a server newer than
 * this extension) degrade to the server's own message rather than showing a
 * bare identifier.
 */
export function describeError(error: RunError | undefined): string {
  if (!error) {
    return "The run failed, but the agent did not say why.";
  }
  if (isKnownErrorCode(error.code)) {
    return SENTENCES[error.code];
  }
  const detail = error.message?.trim();
  return detail
    ? `The agent reported an error this version of the extension does not recognise: ${detail}`
    : "The agent reported an error this version of the extension does not recognise.";
}

/** True when re-submitting the same form could plausibly succeed. */
export function isRetryable(error: RunError | undefined): boolean {
  if (!error) return false;
  return error.code === "internal" || error.code === "timeout" || error.code === "clone_failed";
}

/** True when the user should change the branch name before retrying. */
export function needsNewBranchName(error: RunError | undefined): boolean {
  return error?.code === "branch_exists";
}
