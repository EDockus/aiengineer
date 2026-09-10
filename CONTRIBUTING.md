# Contributing

Thanks for looking. This is a small, opinionated project: one work item in, one pull
request out, no keys anywhere. Changes that keep it that way are very welcome.

Before anything substantial, open an issue. It is cheaper to disagree about an approach in
an issue than in a review.

**`docs/` is the specification.** [`docs/contract.md`](docs/contract.md) is authoritative
for anything a caller can see — the request, the response and the error codes — and
[`docs/architecture.md`](docs/architecture.md) is authoritative for everything behind it:
the runtime and protocol, the run sequence and its invariants, the harness abstraction, the
prompt, the container and what the test suite has to cover.
[`docs/configuration.md`](docs/configuration.md) and [`docs/deploy.md`](docs/deploy.md)
own the settings and the deployment. Where this file and one of those disagree, the doc
wins — and say so in your pull request rather than quietly picking one.

## Development setup

### The agent

Python 3.14.

```bash
cd agent
uv venv --python 3.14 .venv
uv pip install '.[dev]'
```

> Do **not** use `uv pip install -e .` with Python 3.14 on macOS. `uv` marks the editable
> `.pth` file with the macOS `UF_HIDDEN` flag and CPython 3.14's `site.addpackage` skips
> hidden `.pth` files, so the install reports success and `import ticket_to_pr` then fails.
> If you want an editable install anyway, run
> `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` after every install.

### The extension

Node 22.

```bash
cd extension
npm ci
npm run build
```

### The sample Function App

Python 3.12 (the Azure Functions runtime version).

```bash
cd examples/function-app
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Running the tests

```bash
cd agent
ruff check .
mypy --strict src/
pytest
```

All three must pass. `pytest -q` is what CI runs.

Nothing in the suite may require model access or a live Azure DevOps organization. Tests
use `FakeHarness` and the local Azure DevOps stub; git tests clone from and push to real
bare repositories over `file://`, which is why CI sets a global git identity.
[`docs/architecture.md §7`](docs/architecture.md#7-testing) says what the suite has to
cover.

Container changes need a build too:

```bash
docker build -t ticket-to-pr agent/
```

On Apple Silicon add `--platform linux/amd64`. The `cryptography` wheel that
`azure-identity` pulls in raises `SIGILL` inside Docker Desktop's aarch64 VM — a Docker
Desktop limitation, not a property of the image. The platform requires `linux/amd64`
anyway.

Other packages:

```bash
cd extension          && npm run typecheck && npm run build && npm run verify
cd examples/function-app && ruff check . && mypy --strict .
```

### Try it end to end without an Azure subscription

[`agent/README.md §3`](agent/README.md#3-run-it-locally-end-to-end) walks through running
the whole orchestration — fetch, clone, branch, code, commit, push, open a pull request —
against a local Azure DevOps stub and a real bare git repository, with the `fake` harness
and no Azure tenant. Do this before you send a change to `run.py`, `ado.py` or `git_ops.py`.

## Adding a harness

A harness is the thing that actually edits the working tree. Adding one is four steps.

**1. Verify the CLI against its current documentation.** Do not guess a flag. The exact
non-interactive invocation and the exact environment variables for Entra-authenticated
Foundry access are things you look up, and the project expects you to record what you
looked up. There is a worked example of the standard in
[`docs/configuration.md §4`](docs/configuration.md#4-harness-configuration--what-was-verified),
and in the module docstrings of `codex.py` and `claude_code.py` — including a case where
running `--help` against the pinned version contradicted the published docs, and the code
follows the binary.

**2. Implement the protocol** in `agent/src/ticket_to_pr/harness/<name>.py` — the rules it
has to keep are in
[`docs/architecture.md §4.2`](docs/architecture.md#42-the-harness-protocol-and-factory):

```python
class Harness(Protocol):
    name: str
    def preflight(self, cfg: Settings) -> None: ...   # CLI present, endpoint configured
    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult: ...
```

Rules that are not negotiable:

- Build the subprocess environment with `clean_env()`. It is an **allowlist**. If your CLI
  needs a variable that is not in `ENV_ALLOWLIST`, pass it explicitly in the `extra` dict —
  do not widen the allowlist unless every harness needs it, and never add anything that
  could carry an Azure DevOps credential.
- Pass the prompt on **stdin**, via `run_cli()`. Never on argv: it is untrusted work-item
  text, it can be tens of kilobytes, and argv is world-readable.
- Run the CLI non-interactively with approvals disabled. A harness that blocks on a prompt
  hangs until `T2PR_MAX_RUN_MINUTES` and then fails as `timeout`.
- Do not commit. `run.py` commits, after checking the diff.
- Return the CLI's own final summary as `HarnessResult.summary` — it becomes the pull
  request body's "what changed" and "how to test".
- Let `run_cli()` map a timeout to the `timeout` contract code. Do not catch it.

**3. Register it.** Add a member to `HarnessName` in `config.py` and a `case` to
`get_harness()` in `harness/__init__.py`. Unknown values must raise `ConfigError` at
startup, not at first request.

**4. Install it in the image.** Add a pinned version to `agent/Dockerfile` — pinned, and
with no `.npmrc`, so no registry token can end up in the image. Print its version at
startup like the others.

Then document it: a row in the `T2PR_HARNESS` line of `docs/configuration.md` and a
subsection in §4 recording what you verified, with the URLs and the dates.

Say in your pull request what the new harness needs covered; the shipped tests are added in
the review pass rather than by the implementer.

## Coding conventions

- **Type hints everywhere.** `mypy --strict` must pass on `agent/src/` and on
  `examples/function-app/`.
- **Pydantic models are the boundary.** Validate at the edge, pass typed objects inward.
- **Map failures to a contract error code where they occur.** Raise
  `RunFailure(ErrorCode.X, "...")`. No generic `except Exception` that flattens a specific
  code into `internal`.
- **The error codes are a closed set for v1.** Do not invent one; see
  [`docs/contract.md §6`](docs/contract.md#6-error-codes).
- **The request contract has no `harness`, `model` or `coder` field**, and unknown fields
  are a `400`. That is the whole security model of "one shared agent" — do not soften it.
- **Never log tokens or `Authorization` headers**, at any level. Never log full work-item
  bodies at `INFO`.
- **Per-run temp directories, always cleaned up**, including on the failure path.
- **Comment the surprising, not the obvious.** Match the density of the surrounding code.
  Where a decision came from a doc page or from documentation you checked, cite it by page
  and section, the way the existing comments do.

Line length is 100. `ruff` settings live in `agent/pyproject.toml` and
`examples/function-app/pyproject.toml`.

## Documentation

Every doc page must be complete for its topic. **No "TBD".** If you genuinely cannot
determine something, resolve it against the vendor documentation and cite what you read;
if it is still ambiguous, say so explicitly and say what you would do about it.

`docs/contract.md` describes a published interface. A breaking change to it bumps the major
version and the path, and needs a `CHANGELOG.md` entry saying so.

## Pull requests

- One concern per pull request.
- Say what you verified and what you could not. "Ran the suite" and "could not test the
  Foundry path without a subscription" are both useful; a claim you did not check is not.
- Run every gate that touches what you changed, and paste the output.
- Update the docs in the same pull request as the code.
- Add a `CHANGELOG.md` entry under `Unreleased`.

Changes go through review by a QA pass and then a security pass; a reviewer may send a
change back with a specific defect — file, line, what is wrong, what fixed looks like.

## License

By contributing you agree that your contributions are licensed under the MIT License, the
same as the rest of the project.
