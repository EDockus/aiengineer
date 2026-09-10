"""Claude Code CLI harness (docs/architecture.md §4.2).

Verified against current documentation on 2026-09-09:

* ``claude -p/--print`` is print (non-interactive) mode and accepts the prompt on stdin
  when piped. https://code.claude.com/docs/en/cli-reference and .../headless
* ``--permission-mode`` accepts ``default``, ``acceptEdits``, ``plan``, ``auto``,
  ``dontAsk``, ``bypassPermissions`` and ``manual``; ``--dangerously-skip-permissions`` is
  documented as equivalent to ``--permission-mode bypassPermissions``. The explicit
  spelling is used here. https://code.claude.com/docs/en/permission-modes
* That same page states the CLI "refuses to start in this mode when running as root or
  under sudo" -- which is why agent/Dockerfile runs as a non-root user.
* ``--output-format {text,json,stream-json}``; the ``json`` object carries the final
  assistant text in ``result``, plus ``subtype``, ``is_error``, ``num_turns`` and
  ``total_cost_usd``. https://code.claude.com/docs/en/headless
* Microsoft Foundry is a first-class provider: ``CLAUDE_CODE_USE_FOUNDRY=1`` with
  ``ANTHROPIC_FOUNDRY_BASE_URL`` (or ``ANTHROPIC_FOUNDRY_RESOURCE``).
  https://code.claude.com/docs/en/microsoft-foundry
* That page documents three auth options. Option C: "Claude Code sends the value of
  ``ANTHROPIC_FOUNDRY_AUTH_TOKEN`` on every request as the ``Authorization: Bearer``
  header. Use this option when another process, such as a host application or a sign-in
  script, has already obtained an access token for you. Requires Claude Code v2.1.203 or
  later." -- agent/Dockerfile pins 2.1.266. It "takes precedence over
  ``ANTHROPIC_FOUNDRY_API_KEY`` and over the default credential chain".

SECURITY NOTE (why not option B, the default credential chain): letting the CLI resolve
its own credential means allow-listing the managed identity's endpoint and header into the
harness environment, and that endpoint mints tokens for *any* audience -- Azure DevOps
included. See the module docstring of ``harness/__init__.py``. The parent mints one
inference-audience token per run and passes its value here instead. The trade-off is that
the token cannot be refreshed mid-run; :func:`ticket_to_pr.harness.foundry_token` warns
when the run budget outlasts it.
* Alias models (``sonnet``/``opus``/``haiku``) do not resolve on Foundry, so
  ``ANTHROPIC_DEFAULT_*_MODEL`` are pinned to the configured deployment name.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..config import Settings
from . import HarnessResult, clean_env, foundry_endpoint, foundry_token, require_cli, run_cli

log = logging.getLogger(__name__)

EXECUTABLE = "claude"


def _anthropic_base_url(project_endpoint: str) -> str:
    """The Anthropic route on the Foundry **account**, derived from the project endpoint.

    Anthropic documents this as ``https://{resource}.services.ai.azure.com/anthropic``
    (https://code.claude.com/docs/en/microsoft-foundry, step 3) -- the account host, with no
    project path. The platform only injects ``FOUNDRY_PROJECT_ENDPOINT``
    (``https://{resource}.services.ai.azure.com/api/projects/{name}``), so the project
    segment has to come off.

    Appending ``/anthropic`` to the project endpoint instead is a plausible-looking mistake
    that fails in a confusing way: the request reaches Foundry's generic project API rather
    than the Anthropic route, and comes back
    ``400 Missing required query parameter: api-version`` -- an error about a parameter
    that the correct route never needs. Verified against a live deployment.
    """
    parsed = urlsplit(project_endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, "/anthropic", "", ""))


class ClaudeCodeHarness:
    name = "claude-code"

    def __init__(self) -> None:
        self._cfg: Settings | None = None
        self._endpoint: str | None = None

    def preflight(self, cfg: Settings) -> None:
        require_cli(EXECUTABLE, self.name)
        self._endpoint = foundry_endpoint()
        self._cfg = cfg
        log.info("claude-code harness ready, model deployment %s", cfg.model_deployment)

    def _settings(self) -> tuple[Settings, str]:
        if self._cfg is None or self._endpoint is None:
            raise RuntimeError("preflight() must run before run()")
        return self._cfg, self._endpoint

    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult:
        cfg, endpoint = self._settings()
        model = cfg.model_deployment
        env = clean_env(
            {
                "HOME": str(repo_dir.parent),
                "CLAUDE_CODE_USE_FOUNDRY": "1",
                "ANTHROPIC_FOUNDRY_BASE_URL": _anthropic_base_url(endpoint),
                # A token, not the means to mint one. Never ANTHROPIC_FOUNDRY_API_KEY
                # (docs/configuration.md §4) and never the identity plumbing
                # (docs/architecture.md §4.2).
                "ANTHROPIC_FOUNDRY_AUTH_TOKEN": foundry_token(run_seconds=timeout_s),
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        argv = [
            EXECUTABLE,
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
        ]
        proc = run_cli(argv, repo_dir=repo_dir, prompt=prompt, timeout_s=timeout_s, env=env)
        summary, failed = _parse_output(proc.stdout)
        # A JSON payload can report failure while the process still exits 0.
        exit_code = proc.returncode or (1 if failed else 0)
        return HarnessResult(
            exit_code=exit_code, summary=summary, stdout=proc.stdout, stderr=proc.stderr
        )


def _parse_output(stdout: str) -> tuple[str, bool]:
    """Extract the final assistant message from ``--output-format json``.

    Returns ``(summary, failed)``. On the error subtypes there is no ``result`` field, so
    ``errors`` is joined instead.
    """
    text = stdout.strip()
    if not text:
        return "", True
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        # Not JSON: hand back the tail rather than losing the harness's own words.
        return text[-4000:], False
    if not isinstance(payload, dict):
        return text[-4000:], False

    failed = bool(payload.get("is_error")) or str(payload.get("subtype", "")) != "success"
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip(), failed
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return "\n".join(str(item) for item in errors), True
    return f"claude-code finished with subtype {payload.get('subtype')!r}", failed
