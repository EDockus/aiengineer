"""Builds the coding prompt from a work item (docs/architecture.md §4.3).

The template is reproduced verbatim from that section. The only additions are the
truncation notices it calls for and an optional "Related work items" block, which is
omitted entirely when the work item has no links.
"""

from __future__ import annotations

import re

from .models import WorkItem
from .redact import redact

#: docs/architecture.md §4.3: "Every field is capped at 8,000 characters".
FIELD_CAP = 8_000

#: Azure DevOps rejects a pull request description longer than 4000 characters, and an
#: over-long one would surface as ``pr_failed`` *after* the branch was pushed. Everything
#: below is sized so the rendered body cannot reach that limit.
DESCRIPTION_CAP = 3_900
SUMMARY_CAP = 2_000
CHANGED_FILES_CAP = 1_000

#: Room for the truncation notice :func:`truncate` appends past its own cap.
_TRUNCATION_SLACK = 64

_BACKTICKS_RE = re.compile(r"`+")

TEMPLATE = """\
You are implementing Azure DevOps work item #{id} ({type}) in this repository.

Title: {title}

Description:
{description}

Acceptance criteria:
{acceptance_criteria}

Repro steps:
{repro_steps}
{related}
Rules:
- Make the smallest change that fully satisfies the work item.
- Follow the repository's existing conventions, linters and test patterns.
- Add or update tests where the repository has them.
- Do not modify CI configuration, secrets, or infrastructure files unless the work item \
explicitly asks.
- Do not create commits; the caller will commit.
- When finished, print a short summary: what changed, how to test, anything left open.
"""


def truncate(text: str, cap: int = FIELD_CAP) -> str:
    """Cap a field and say so in-band, so the model knows it is not seeing everything."""
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return f"{text[:cap]}\n\n[truncated: {dropped} more characters omitted]"


def build_prompt(work_item: WorkItem, *, cap: int = FIELD_CAP) -> str:
    related = ""
    if work_item.linked_titles:
        joined = "\n".join(f"- {title}" for title in work_item.linked_titles)
        related = f"\nRelated work items:\n{joined}\n"

    return TEMPLATE.format(
        id=work_item.id,
        type=work_item.type or "Work Item",
        title=truncate(work_item.title, cap),
        description=truncate(work_item.description, cap) or "none provided",
        acceptance_criteria=truncate(work_item.acceptance_criteria, cap) or "none provided",
        repro_steps=truncate(work_item.repro_steps, cap) or "n/a",
        related=related,
    )


def pr_title(work_item: WorkItem) -> str:
    """``<type> #<id>: <title>`` (docs/architecture.md §3 steps 8 and 9)."""
    return f"{work_item.type or 'Work Item'} #{work_item.id}: {work_item.title}".strip()


def clamp_backticks(text: str) -> str:
    """Collapse every run of more than three backticks in ``text`` down to three.

    Backtick runs in the harness summary carry no information a reviewer needs, but a long
    one drives :func:`fence`'s marker arbitrarily wide: a summary ending in 2,999 backticks
    produces a 3,000-backtick marker and a description that blows past ADO's 4,000-char
    limit even though the body was capped, turning the run into a ``pr_failed`` after the
    branch was pushed. Clamping the body first pins the marker at four backticks, so the
    length budget in :func:`pr_description` -- computed from an empty render -- is a true
    upper bound again.
    """
    return _BACKTICKS_RE.sub(lambda m: "`" * min(len(m.group()), 3), text)


def fence(text: str) -> str:
    """Wrap harness output in a code fence it cannot break out of.

    The summary is model output shaped by a work-item description, which anyone who can
    edit a ticket controls. Rendered as markdown in a pull request description -- the one
    field every reviewer reads before merging -- it can forge headings or plant a link
    reading "Approved by the security team". Inside a fence it is just text. The fence is
    one backtick longer than the longest run in the body, so no embedded fence closes it;
    callers that need a bounded marker length clamp the body with :func:`clamp_backticks`
    first, which pins it at four.
    """
    longest = max((len(run) for run in _BACKTICKS_RE.findall(text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}text\n{text}\n{marker}"


def pr_description(work_item: WorkItem, *, summary: str, files: list[str]) -> str:
    """PR body: summary, what changed, how to test, and the AB# link ADO needs.

    The harness summary is redacted, capped and fenced: it is untrusted text sitting next
    to a heading a reviewer is meant to trust.
    """
    shown = files[:50]
    changed = "\n".join(f"- `{path}`" for path in shown) or "- (no files listed)"
    if len(files) > len(shown):
        changed += f"\n- ... and {len(files) - len(shown)} more"
    changed = truncate(changed, CHANGED_FILES_CAP)
    # Clamp before truncating/fencing so the fence marker is bounded (see clamp_backticks);
    # a wide marker is the one way the rendered body can exceed the length budget below.
    body = clamp_backticks(redact(summary).strip()) or (
        "The coding harness did not produce a summary."
    )

    def render(text: str) -> str:
        return (
            f"Automated change for work item #{work_item.id}.\n\n"
            f"## Summary\n\n{fence(text)}\n\n"
            f"## What changed\n\n{changed}\n\n"
            "## How to test\n\n"
            "See the summary above; run the repository's own test suite against this "
            "branch.\n\n"
            f"Fixes AB#{work_item.id}\n"
        )

    budget = min(SUMMARY_CAP, DESCRIPTION_CAP - len(render("")) - _TRUNCATION_SLACK)
    return render(truncate(body, max(budget, 0)))


def commit_message(work_item: WorkItem, *, summary: str) -> str:
    return f"{pr_title(work_item)}\n\n{truncate(redact(summary).strip(), SUMMARY_CAP)}\n"
