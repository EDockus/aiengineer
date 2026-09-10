# ticket-to-pr agent

The Foundry hosted agent: it turns an Azure DevOps work item into a branch and a pull
request. This page covers building it, and running it end to end **on a laptop with no
Foundry project and no Azure DevOps organization**.

Everything below was executed on macOS 15 (Apple Silicon) with Python 3.14.5, uv 0.12.1,
git 2.47.1 and Docker 27.3.1.

---

## 1. Install

```bash
cd agent
uv venv --python 3.14 .venv
uv pip install '.[dev]'
```

> **Do not use `uv pip install -e .` with Python 3.14 on macOS.** uv marks the editable
> `.pth` file it drops in `site-packages` with the macOS `UF_HIDDEN` flag, and CPython
> 3.14's `site.addpackage` now skips hidden `.pth` files. The install reports success and
> `import ticket_to_pr` then fails. If you want an editable install anyway, run
> `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` after every install.

> **Because that install is a copy, not a link, `.venv/bin/ticket-to-pr` keeps running the
> source as it was at install time.** Edit `src/` and the tests will pick the change up
> immediately — they import from `src/` — while the running agent silently will not. The
> symptom is a route or a flag that the test suite proves exists but a live `curl` returns
> `404` for. After changing anything under `src/`, reinstall:
>
> ```bash
> uv pip install --python .venv/bin/python --no-deps --reinstall .
> ```

## 2. Gates

```bash
cd agent
.venv/bin/ruff check .
.venv/bin/mypy --strict src/
```

```
All checks passed!
Success: no issues found in 12 source files
```

Container build, from the repository root:

```bash
docker build -t ticket-to-pr agent/
```

The image is `linux/amd64`-first. On an Apple Silicon host, build and run it with
`--platform linux/amd64`: the `cryptography` wheel that `azure-identity` pulls in raises
`SIGILL` inside Docker Desktop's aarch64 VM. That is a Docker Desktop limitation, not a
property of the image.

```bash
docker build --platform linux/amd64 -t ticket-to-pr agent/
docker run --rm --platform linux/amd64 -p 8020:8000 \
  -e T2PR_ALLOWED_ORGS=contoso \
  -e FOUNDRY_PROJECT_ENDPOINT=https://example.services.ai.azure.com/api/projects/demo \
  ticket-to-pr
```

---

## 3. Run it locally, end to end

`scripts/dev_ado_stub.py` is a local stand-in for Azure DevOps. It serves the handful of
REST endpoints the agent calls and backs the git side with a **real bare repository** on
disk that the agent clones from and pushes to over `file://`. The production `ado.py` and
`git_ops.py` do genuine work against it; nothing is mocked out inside the agent.

Paired with the `fake` harness ([docs/architecture.md §7](../docs/architecture.md#7-testing)),
that gives you the whole
orchestration — fetch, clone, branch, code, commit, push, open a PR — with no Azure
tenant and no model.

### Terminal 1 — the Azure DevOps stub

```bash
cd agent
.venv/bin/python scripts/dev_ado_stub.py --reset
```

```
seeded bare repository at /path/to/agent/.dev-ado/payments-api.git
dev ADO stub on http://127.0.0.1:8080  (org=contoso project=Platform repo=payments-api)
```

It binds to loopback and refuses any other bind address. It seeds one repo,
`payments-api`, with a `main` branch and two files.

### Terminal 2 — the agent

```bash
cd agent
T2PR_DEV_MODE=i-understand-this-disables-auth \
T2PR_ADO_BASE_URL=http://127.0.0.1:8080 \
T2PR_HARNESS=fake \
T2PR_ALLOWED_ORGS=contoso \
T2PR_PORT=8010 \
.venv/bin/ticket-to-pr
```

```
WARNING ticket_to_pr.main running with the fake harness; no model will be called
WARNING ticket_to_pr.ado dev mode: using a static Azure DevOps token against http://127.0.0.1:8080
INFO    ticket_to_pr.main cli git: ['git version 2.47.1']
INFO    ticket_to_pr.main cli node: ['v23.11.0']
INFO    ticket_to_pr.main ticket-to-pr ready: protocol 1.0.0, harness fake, model gpt-6, orgs ['contoso'], dev_mode=True
WARNING ticket_to_pr.main DEV MODE IS ON: ... Never use this configuration anywhere but a developer machine.
INFO:   Uvicorn running on http://127.0.0.1:8010
```

### Terminal 3 — drive it

```bash
curl -s http://127.0.0.1:8010/healthz
```
```json
{"status":"ok","protocol":"1.0.0","harness":"fake"}
```

```bash
curl -s -X POST http://127.0.0.1:8010/invocations \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "1",
    "organization": "contoso",
    "project": "Platform",
    "workItem": { "type": "Bug", "id": 4123 },
    "repository": "payments-api",
    "branch": "bugfix/4123-null-ref",
    "baseBranch": "main",
    "correlationId": "readme-verify"
  }'
```
```json
{"runId":"690cd3bc-4098-4dff-a94a-e275088f3738","status":"queued","correlationId":"readme-verify"}
```

Poll for status (the run finishes in well under a second with the fake harness):

```bash
RUN=690cd3bc-4098-4dff-a94a-e275088f3738
curl -s http://127.0.0.1:8010/invocations/$RUN | python3 -m json.tool
```
```json
{
    "runId": "690cd3bc-4098-4dff-a94a-e275088f3738",
    "status": "succeeded",
    "phase": "done",
    "startedAt": "2026-09-09T06:45:42.702135Z",
    "finishedAt": "2026-09-09T06:45:42.892770Z",
    "result": {
        "pullRequestId": 801,
        "pullRequestUrl": "http://127.0.0.1:8080/contoso/Platform/_git/payments-api/pullrequest/801",
        "branch": "bugfix/4123-null-ref",
        "commits": 1,
        "filesChanged": 1
    },
    "error": null,
    "correlationId": "readme-verify"
}
```

Observe the pull request the agent opened in the local fake:

```bash
curl -s http://127.0.0.1:8080/_dev/pullrequests | python3 -m json.tool
```
```json
{
  "branches": {
    "refs/heads/bugfix/4123-null-ref": "707f0342b97194307a3d978dde99425e98aa2404",
    "refs/heads/main": "dc19bbc1eb303a48a56c8fe58f5f2e0c910d663a"
  },
  "pullRequests": [
    {
      "pullRequestId": 801,
      "title": "Bug #4123: Null reference when charging a zero-amount order",
      "sourceRefName": "refs/heads/bugfix/4123-null-ref",
      "targetRefName": "refs/heads/main",
      "isDraft": true
    }
  ]
}
```

And confirm the branch really exists as git objects, pushed by the real `git_ops.push`:

```bash
git --git-dir=.dev-ado/payments-api.git log --oneline --name-status bugfix/4123-null-ref -1
```
```
707f034 Bug #4123: Null reference when charging a zero-amount order
A	TICKET_TO_PR.md
```

### Error paths you can exercise against the stub

| Request change | Result |
|---|---|
| add any extra field, e.g. `"harness": "codex"` | HTTP 400, `Extra inputs are not permitted` |
| `"branch": "-rf; echo pwned"` | HTTP 400, `must match ^[A-Za-z0-9._/-]+$` |
| `"organization": "fabrikam"` | run fails, `ado_forbidden` |
| re-post the same `branch` | run fails, `branch_exists` |
| `"workItem": {"type":"Bug","id":9999}` | run fails, `work_item_not_found` |
| `"repository": "nope"` | run fails, `repository_not_found` |
| `"workItem": {"type":"Nonsense","id":4123}` | run fails, `work_item_not_found` |

---

## 4. Configuration

The variables from [docs/configuration.md §1](../docs/configuration.md#1-settings), plus two
that are not in that table. Both of the extra ones exist only to make section 3 above
possible, and both are documented in
[docs/configuration.md §3](../docs/configuration.md#3-dev-only-variables).

| Variable | Default | Notes |
|---|---|---|
| `T2PR_HARNESS` | `codex` | `codex`, `claude-code`, or `fake` (dev mode only) |
| `T2PR_MODEL_DEPLOYMENT` | `gpt-6` | Model deployment name in the Foundry project |
| `T2PR_ALLOWED_ORGS` | *(required)* | Comma-separated ADO org allowlist. Startup fails if empty |
| `T2PR_MAX_RUN_MINUTES` | `20` | Hard timeout for the harness step |
| `T2PR_MAX_FILES_CHANGED` | `50` | Abort with `harness_failed` above this |
| `T2PR_PR_DRAFT` | `true` | Create the PR as a draft |
| `T2PR_PR_REVIEWERS` | *(empty)* | Comma-separated ADO user/group ids |
| `T2PR_COMMIT_AUTHOR_NAME` | `Ticket-to-PR agent` | |
| `T2PR_COMMIT_AUTHOR_EMAIL` | `ticket-to-pr@noreply.local` | |
| `T2PR_LOG_LEVEL` | `INFO` | |
| `T2PR_HOST` | `0.0.0.0` | `127.0.0.1` in dev mode |
| `T2PR_PORT` | `8000` | |
| **`T2PR_ADO_BASE_URL`** | `https://dev.azure.com` | **Dev mode only.** |
| **`T2PR_DEV_MODE`** | *(unset)* | **Dev mode only.** See below |

### How dev mode fails closed

`T2PR_DEV_MODE` disables Entra authentication for Azure DevOps, so it is guarded rather
than merely defaulted off. All of the following must hold or the process refuses to start:

1. `T2PR_DEV_MODE` is exactly `i-understand-this-disables-auth`. Any other non-empty
   value — `1`, `true`, `yes` — is a startup **error**, not a silent "off", so a
   half-remembered setting is loud instead of ambiguous.
2. `/etc/ticket-to-pr/production` does not exist. `agent/Dockerfile` creates that file, so
   dev mode is unavailable in any built image without rebuilding it.
3. None of `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_PROJECT_ENDPOINT`, `CONTAINER_APP_NAME`,
   `CONTAINER_APP_ENV_DNS_SUFFIX`, `WEBSITE_INSTANCE_ID`, `WEBSITE_SITE_NAME`,
   `KUBERNETES_SERVICE_HOST`, `IDENTITY_ENDPOINT`, `MSI_ENDPOINT` or
   `AZURE_FEDERATED_TOKEN_FILE` is set — i.e. this is not a cloud host.
4. The listener binds to loopback. Dev mode defaults `T2PR_HOST` to `127.0.0.1`; an
   *explicit* non-loopback `T2PR_HOST` is refused.
5. `T2PR_ADO_BASE_URL` is set and is an `http(s)` URL on `127.0.0.1`, `localhost` or
   `::1`. Without it the agent would send a placeholder token to the real
   `dev.azure.com`.

Conversely, with dev mode off, `T2PR_ADO_BASE_URL`, `T2PR_DEV_ADO_TOKEN` and
`T2PR_HARNESS=fake` are each a hard startup error rather than a silently ignored setting.

---

## 5. Harness notes

`T2PR_HARNESS` selects one of three implementations of the `Harness` protocol in
`src/ticket_to_pr/harness/`.

**`codex`** — `codex exec` with the prompt on stdin, `--sandbox workspace-write
--skip-git-repo-check`, and the Foundry provider supplied through repeatable
`-c key=value` config flags. `codex exec` rejects `--ask-for-approval` (it exists only on
the top-level `codex` command), so the approval policy is set with
`-c approval_policy="never"`. The bearer token comes from
`model_providers.foundry.env_key`: the parent process mints one inference-scoped Entra
token per run and puts it in `FOUNDRY_AUTH_TOKEN`, so the harness never holds the means to
mint a token of its own. **Caveat:** Microsoft's own Codex-on-Foundry page states "Entra ID
support is currently not available for Codex". Every configuration key used here is
documented, but the Foundry-plus-Entra *combination* is not a documented, tested flow.
Smoke-test it.

**`claude-code`** — `claude --print --output-format json --permission-mode
bypassPermissions --model <deployment>`, with `CLAUDE_CODE_USE_FOUNDRY=1` and
`ANTHROPIC_FOUNDRY_BASE_URL`. `ANTHROPIC_FOUNDRY_API_KEY` is never set — model access is
keyless ([SECURITY.md → Credentials](../SECURITY.md#credentials));
`ANTHROPIC_FOUNDRY_AUTH_TOKEN` carries the inference-scoped Entra token the parent minted
for this run, rather than letting the CLI resolve the managed identity itself — resolving
it would mean handing the harness the identity endpoint, which mints tokens for any
audience including Azure DevOps. Foundry does not resolve the `sonnet`/`opus`/`haiku`
aliases, so `ANTHROPIC_DEFAULT_*_MODEL` are all pinned to `T2PR_MODEL_DEPLOYMENT`.

Neither token can be refreshed mid-run. Keep `T2PR_MAX_RUN_MINUTES` well inside the
lifetime of an Entra access token (typically an hour or more); the agent logs a warning if
the token it minted expires before the run budget does.

**`fake`** — writes one deterministic file and returns a canned summary. Dev mode only.

Whichever is selected, the harness subprocess environment is built from an **allowlist**
(`harness/__init__.py`, `ENV_ALLOWLIST`), not by filtering `os.environ`. No Azure DevOps
token, no `T2PR_*` setting and no `GIT_CONFIG_*` value can reach it, and
`git_ops.strip_credential_config` clears `.git/config` of credential helpers immediately
before the harness starts.

---

## 6. Layout

```
agent/
├── Dockerfile                     python:3.14-slim, Node LTS, both CLIs, non-root
├── pyproject.toml                 deps, ruff, mypy --strict, pytest
├── scripts/
│   └── dev_ado_stub.py            local Azure DevOps stand-in (dev only, not in the image)
└── src/ticket_to_pr/
    ├── main.py                    Invocations server: POST /invocations, GET status, /healthz
    ├── config.py                  Settings.from_env, the dev-mode gate
    ├── models.py                  wire contract + domain models + RunFailure
    ├── ado.py                     Azure DevOps REST client
    ├── git_ops.py                 clone, branch, commit, push, credential hygiene
    ├── prompt.py                  prompt, PR title/body, commit message
    ├── run.py                     orchestration and the run store
    └── harness/                   protocol, factory, codex, claude_code, fake
```

The shipped test suite lives in `agent/tests/`; what it has to cover is in
[docs/architecture.md §7](../docs/architecture.md#7-testing).
