import { BRANCH_NAME_PATTERN } from "./contract.js";

const MAX_TITLE_SLUG = 48;
const MAX_BRANCH_LENGTH = 200;

function slugify(value: string, maxLength: number): string {
  const slug = value
    .toLowerCase()
    .normalize("NFKD")
    // Drop combining marks so "Über" becomes "uber" rather than "ber".
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (slug.length <= maxLength) return slug;
  // Cut on a word boundary where one is close enough to the limit.
  const cut = slug.slice(0, maxLength);
  const lastDash = cut.lastIndexOf("-");
  return (lastDash > maxLength - 12 ? cut.slice(0, lastDash) : cut).replace(/-+$/, "");
}

/**
 * Default branch name: `{type-slug}/{id}-{title-slug}` (docs/extension.md §6).
 * Falls back gracefully when the type or title slugify to nothing.
 */
export function suggestBranchName(workItem: { id: number; type: string; title: string }): string {
  const typeSlug = slugify(workItem.type, 24) || "work";
  const titleSlug = slugify(workItem.title, MAX_TITLE_SLUG);
  return titleSlug ? `${typeSlug}/${workItem.id}-${titleSlug}` : `${typeSlug}/${workItem.id}`;
}

export interface BranchValidation {
  valid: boolean;
  /** A sentence explaining the problem, or undefined when valid. */
  message?: string;
}

/**
 * Client-side branch validation.
 *
 * The contract rule is `^[A-Za-z0-9._/-]+$` and that is the primary check. The
 * additional checks below reject names that satisfy the contract pattern but
 * that git itself will not accept as a ref, so the user finds out here rather
 * than after a round trip. The server remains the authority: `branch_exists`
 * in particular can only be detected there and is handled in the status panel.
 */
export function validateBranchName(value: string): BranchValidation {
  const name = value.trim();
  if (!name) {
    return { valid: false, message: "Enter a branch name." };
  }
  if (!BRANCH_NAME_PATTERN.test(name)) {
    return {
      valid: false,
      message:
        "Branch names can only contain letters, numbers, and the characters . _ / and - (no spaces).",
    };
  }
  if (name.length > MAX_BRANCH_LENGTH) {
    return { valid: false, message: `Branch names must be ${MAX_BRANCH_LENGTH} characters or fewer.` };
  }
  if (name.startsWith("/") || name.endsWith("/")) {
    return { valid: false, message: "Branch names cannot start or end with a slash." };
  }
  if (name.includes("//")) {
    return { valid: false, message: "Branch names cannot contain two slashes in a row." };
  }
  if (name.includes("..")) {
    return { valid: false, message: "Branch names cannot contain two dots in a row." };
  }
  if (name.endsWith(".") || name.endsWith(".lock") || name.split("/").some((s) => s.endsWith(".lock"))) {
    return { valid: false, message: "Branch names cannot end with a dot or with “.lock”." };
  }
  if (name.split("/").some((segment) => segment.startsWith("."))) {
    return { valid: false, message: "No part of a branch name can start with a dot." };
  }
  return { valid: true };
}
