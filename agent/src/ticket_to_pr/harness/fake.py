"""A harness that writes one deterministic file and never calls a model.

docs/architecture.md §7 makes this a shipped component, not test scaffolding: it is what
lets the whole orchestration run without model access.
It is selectable at runtime only while dev mode is on -- see
:func:`ticket_to_pr.config.Settings.from_env`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Settings
from . import HarnessResult

MARKER_FILE = "TICKET_TO_PR.md"


class FakeHarness:
    name = "fake"

    def __init__(self, marker_file: str = MARKER_FILE) -> None:
        self._marker_file = marker_file
        self._cfg: Settings | None = None

    def preflight(self, cfg: Settings) -> None:
        # Nothing to verify: no CLI, no model, no network.
        self._cfg = cfg

    def run(self, repo_dir: Path, prompt: str, timeout_s: int) -> HarnessResult:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        target = repo_dir / self._marker_file
        target.write_text(
            "# Ticket-to-PR agent\n\n"
            "This file was written by the fake coding harness, which makes a single\n"
            "deterministic change instead of calling a model.\n\n"
            f"Prompt digest: `{digest}`\n",
            encoding="utf-8",
        )
        summary = (
            f"Wrote `{self._marker_file}` describing the requested change "
            f"(prompt digest {digest}).\n\n"
            "How to test: open the file and confirm it exists on this branch. "
            "Nothing else was modified.\n\n"
            "Left open: this is the fake harness; no real implementation was produced."
        )
        return HarnessResult(exit_code=0, summary=summary, stdout=summary, stderr="")
