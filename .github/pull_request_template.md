<!--
CONTRIBUTING.md is the long version of this checklist:
https://github.com/EDockus/aiengineer/blob/main/CONTRIBUTING.md
One concern per pull request. Delete any section that genuinely does not apply.
-->

## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Why

<!-- The doc section (e.g. `docs/contract.md` §6), the issue, or the failure this came out of. -->

## What I verified

<!--
Paste the output, not a claim. Be explicit about what you could NOT check — "could not
test the Foundry model path without a subscription" is a useful review input; a gate you
did not run and did not mention is not.
-->

- [ ] `cd agent && ruff check . && mypy --strict src/ && pytest`
- [ ] `cd extension && npm run verify` (30/30) — if `extension/` changed
- [ ] `docker build --platform linux/amd64 -t ticket-to-pr agent/` — if the image or its dependencies changed
- [ ] `shellcheck -x scripts/*.sh` — if `scripts/` changed
- [ ] Ran it end to end against the local Azure DevOps stub (`agent/README.md §3`) — if `run.py`, `ado.py` or `git_ops.py` changed

Could not verify:

<!-- List it here. Empty is fine; unstated is not. -->

## The rules this touches

<!-- Tick only what applies; each one is a blocking defect if broken. They come from
     CONTRIBUTING.md ("Coding conventions"), `docs/contract.md` and `docs/architecture.md`;
     read the section before ticking its box. -->

- [ ] No secret is logged, echoed into a pull request body or commit message, passed in argv, or put in a harness environment. `T2PR_ADO_PAT` in particular.
- [ ] The request contract still has no `harness`, `model` or `coder` field, and unknown fields still return `400`.
- [ ] No new error code — the v1 set of eleven is closed.
- [ ] A failed pull request creation still leaves no dangling remote branch (SECURITY.md documents the one deliberate exception).
- [ ] No secret is baked into the container image.

## Docs and changelog

- [ ] Docs updated in this same pull request (`docs/contract.md` is a published interface — a breaking change bumps the major version and the path).
- [ ] `CHANGELOG.md` entry added under `Unreleased`.
