"""Codex CLI harness (docs/architecture.md §4.2).

Verified against current documentation on 2026-09-09:

* ``codex exec`` is the non-interactive subcommand; with the prompt argument omitted it
  reads the prompt from stdin, which is how this module passes it.
  https://learn.chatgpt.com/docs/non-interactive-mode
* ``--sandbox {read-only,workspace-write,danger-full-access}`` selects the sandbox.
  https://learn.chatgpt.com/docs/agent-approvals-security
* CORRECTION to those docs, verified by running ``codex exec --help`` against the pinned
  codex-cli 0.153.4 in this image: ``--ask-for-approval`` is accepted by the top-level
  ``codex`` command but *not* by ``codex exec``, which rejects it with "unexpected
  argument". The approval policy is set through the ``approval_policy`` config key instead.
* ``-m/--model``, ``-C/--cd``, ``--skip-git-repo-check``, ``--output-last-message`` and the
  repeatable ``-c/--config key=value`` (TOML-valued) are all documented in
  https://learn.chatgpt.com/docs/cli/reference
* Provider configuration keys ``model_provider`` and ``model_providers.<id>.{name,
  base_url,wire_api,env_key}`` are documented in
  https://learn.chatgpt.com/docs/config-file/config-reference. ``env_key`` "specifies
  which environment variable contains the provider API key"; Codex reads that variable at
  request time and sends its value as the ``Authorization: Bearer`` header, which is what
  a Foundry endpoint wants from an Entra token.

SECURITY NOTE (why this is not ``auth.command``): the command-backed
``model_providers.<id>.auth.{command,timeout_ms,refresh_interval_ms}`` block is also
documented and does refresh, but making it work required allow-listing the managed
identity's own endpoint and headers into the harness environment -- and that endpoint
mints tokens for *any* audience, including Azure DevOps. See the module docstring of
``harness/__init__.py``. The parent mints one inference-audience token per run instead and
passes only its value; the cost is that the token cannot be refreshed mid-run, which
:func:`ticket_to_pr.harness.foundry_token` warns about when the run budget is longer than
the token's remaining lifetime.

NOT VERIFIED, and called out in the report and docs/configuration.md: Microsoft's own
Codex-on-Foundry page (learn.microsoft.com/azure/foundry/openai/how-to/codex, ms.date
2026-09-03) states "Entra ID support is currently not available for Codex", and
openai/codex issue #2024 (still open) asks for exactly this. The bearer-token path below
uses only documented, provider-agnostic configuration keys, but the *combination* of
Foundry plus an Entra bearer token is not a documented, tested flow. If it 401s, that is
the reason.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Settings
from . import (
    HarnessResult,
    clean_env,
    foundry_endpoint,
    foundry_token,
    harness_scratch_dir,
    require_cli,
    run_cli,
)

log = logging.getLogger(__name__)

EXECUTABLE = "codex"
PROVIDER_ID = "foundry"

#: Environment variable Codex reads the bearer token out of, named by ``env_key`` below.
#: The parent fills it with an inference-audience token; nothing else in the harness
#: environment can produce another one.
TOKEN_ENV = "FOUNDRY_AUTH_TOKEN"  # noqa: S105 - a variable name, not a secret


def _toml(value: object) -> str:
    """Render a Python value as the TOML literal ``-c key=value`` expects."""
    return json.dumps(value)


class CodexHarness:
    name = "codex"

    def __init__(self) -> None:
        self._cfg: Settings | None = None
        self._endpoint: str | None = None

    def preflight(self, cfg: Settings) -> None:
        require_cli(EXECUTABLE, self.name)
        self._endpoint = foundry_endpoint()
        self._cfg = cfg
        log.info("codex harness ready, model deployment %s", cfg.model_deployment)

    def _settings(self) -> tuple[Settings, str]:
        if self._cfg is None or self._endpoint is None:
            raise RuntimeError("preflight() must run before run()")
        return self._cfg, self._endpoint

    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult:
        cfg, endpoint = self._settings()
        provider = f"model_providers.{PROVIDER_ID}"

        # Not a bare mkdtemp: the CLI runs at the harness uid and could not write one
        # owned 0700 by this process -- --output-last-message would fail with no error and
        # the summary would silently degrade to stdout. See harness.harness_scratch_dir.
        with harness_scratch_dir(repo_dir, "codex-") as scratch:
            last_message = scratch / "final.txt"
            argv = [
                EXECUTABLE,
                "exec",
                # Ignore anything a developer left in ~/.codex so the run is reproducible.
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "--cd",
                str(repo_dir),
                "--model",
                cfg.model_deployment,
                "--output-last-message",
                str(last_message),
                # VERIFIED against codex-cli 0.153.4: --ask-for-approval exists on the
                # top-level `codex` command but NOT on `codex exec`, which rejects it as an
                # unexpected argument. The equivalent is the approval_policy config key.
                "-c",
                f"approval_policy={_toml('never')}",
                "-c",
                f"model_provider={_toml(PROVIDER_ID)}",
                "-c",
                f"{provider}.name={_toml('Microsoft Foundry')}",
                "-c",
                f"{provider}.base_url={_toml(endpoint + '/openai/v1')}",
                "-c",
                f"{provider}.wire_api={_toml('responses')}",
                # Read the bearer token out of the environment rather than letting codex
                # mint one: the harness never gets the identity plumbing
                # (docs/architecture.md §4.2).
                "-c",
                f"{provider}.env_key={_toml(TOKEN_ENV)}",
            ]
            # HOME must be writable: codex keeps its session state there.
            env = clean_env(
                {
                    "HOME": str(repo_dir.parent),
                    "CODEX_HOME": str(scratch),
                    TOKEN_ENV: foundry_token(run_seconds=timeout_s),
                }
            )
            proc = run_cli(
                argv, repo_dir=repo_dir, prompt=prompt, timeout_s=timeout_s, env=env
            )
            summary = _read_summary(last_message, proc.stdout)

        return HarnessResult(
            exit_code=proc.returncode,
            summary=summary,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def _read_summary(last_message: Path, stdout: str) -> str:
    """Prefer the file --output-last-message wrote; fall back to stdout.

    ``codex exec`` streams progress to stderr and prints only the final agent message to
    stdout, so the fallback is still the summary and not a transcript.
    """
    if last_message.exists():
        text = last_message.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return stdout.strip()[-4000:]
