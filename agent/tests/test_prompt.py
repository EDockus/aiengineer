"""Prompt construction, HTML stripping and truncation (docs/architecture.md §4.3)."""

from __future__ import annotations

import pytest

from ticket_to_pr.ado import strip_html
from ticket_to_pr.models import WorkItem
from ticket_to_pr.prompt import (
    FIELD_CAP,
    build_prompt,
    commit_message,
    pr_description,
    pr_title,
    truncate,
)

ITEM = WorkItem(
    id=4123,
    type="Bug",
    title="Null reference when charging a zero-amount order",
    description="charge() raises NullReferenceException for amount=0.",
    acceptance_criteria="- charge(0) returns 0\n- a regression test covers it",
    repro_steps="1. POST /charge amount=0\n2. Observe the 500",
    tags=("agent", "payments"),
)


# ------------------------------------------------------------------------- the template


def test_prompt_contains_every_line_the_spec_mandates() -> None:
    text = build_prompt(ITEM)
    for line in (
        "You are implementing Azure DevOps work item #4123 (Bug) in this repository.",
        "Title: Null reference when charging a zero-amount order",
        "Description:",
        "Acceptance criteria:",
        "Repro steps:",
        "Rules:",
        "- Make the smallest change that fully satisfies the work item.",
        "- Follow the repository's existing conventions, linters and test patterns.",
        "- Add or update tests where the repository has them.",
        "- Do not create commits; the caller will commit.",
        "- When finished, print a short summary: what changed, how to test, "
        "anything left open.",
    ):
        assert line in text
    assert (
        "- Do not modify CI configuration, secrets, or infrastructure files unless the "
        "work item explicitly asks." in text
    )


def test_prompt_carries_the_field_values() -> None:
    text = build_prompt(ITEM)
    assert ITEM.description in text
    assert "charge(0) returns 0" in text
    assert "POST /charge amount=0" in text


def test_empty_optional_fields_get_the_spec_placeholders() -> None:
    text = build_prompt(WorkItem(id=1, type="Task", title="T"))
    assert "Acceptance criteria:\nnone provided" in text
    assert "Repro steps:\nn/a" in text
    assert "Description:\nnone provided" in text


def test_a_typeless_work_item_still_renders() -> None:
    text = build_prompt(WorkItem(id=1, type="", title="T"))
    assert "#1 (Work Item)" in text


def test_related_work_items_appear_only_when_present() -> None:
    assert "Related work items" not in build_prompt(ITEM)
    linked = ITEM.model_copy(update={"linked_titles": ("#4100 Payments hardening",)})
    text = build_prompt(linked)
    assert "Related work items:\n- #4100 Payments hardening" in text


def test_the_prompt_never_carries_credentials_or_config() -> None:
    """The harness gets repo files and the work item, nothing else (docs/architecture.md §4.2)."""
    text = build_prompt(ITEM).casefold()
    for word in ("bearer", "authorization", "t2pr_", "token", "credential", "password"):
        assert word not in text


# ---------------------------------------------------------------------------- truncation


def test_truncate_is_a_no_op_below_the_cap() -> None:
    assert truncate("x" * FIELD_CAP) == "x" * FIELD_CAP
    assert "[truncated" not in truncate("x" * FIELD_CAP)


def test_truncate_caps_and_says_so() -> None:
    out = truncate("x" * (FIELD_CAP + 500))
    assert out.startswith("x" * FIELD_CAP)
    assert "[truncated: 500 more characters omitted]" in out


def test_each_field_is_truncated_independently_with_the_note_present() -> None:
    """docs/architecture.md §4.3: cap each field at 8k and note the truncation in the prompt."""
    big = WorkItem(
        id=9,
        type="Bug",
        title="t",
        description="d" * (FIELD_CAP + 10),
        acceptance_criteria="a" * (FIELD_CAP + 20),
        repro_steps="r" * (FIELD_CAP + 30),
    )
    text = build_prompt(big)
    assert text.count("[truncated:") == 3
    assert "[truncated: 10 more characters omitted]" in text
    assert "[truncated: 20 more characters omitted]" in text
    assert "[truncated: 30 more characters omitted]" in text
    assert "d" * (FIELD_CAP + 1) not in text


def test_a_huge_title_is_capped_too() -> None:
    text = build_prompt(WorkItem(id=9, type="Bug", title="t" * (FIELD_CAP + 1)))
    assert "[truncated: 1 more characters omitted]" in text


def test_the_cap_is_configurable_for_callers_that_need_a_smaller_one() -> None:
    text = build_prompt(ITEM, cap=10)
    assert "[truncated:" in text


# -------------------------------------------------------------------------- HTML stripping


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>hello</p>", "hello"),
        ("<div><b>bold</b> text</div>", "bold text"),
        ("line one<br>line two", "line one\nline two"),
        ("line one<br />line two", "line one\nline two"),
        ("<ul><li>a</li><li>b</li></ul>", "- a\n- b"),
        ("<ol><li>step</li></ol>", "- step"),
        ("a &amp; b &quot;c&quot; &lt;d&gt;", 'a & b "c" <d>'),
        ("<h1>Title</h1><p>Body</p>", "Title\nBody"),
        ("", ""),
        (None, ""),
        (0, ""),
        ("   \n  ", ""),
    ],
)
def test_strip_html_flattens_ado_rich_text(raw: object, expected: str) -> None:
    assert strip_html(raw) == expected


def test_strip_html_removes_script_and_style_markup() -> None:
    out = strip_html("<script>alert(1)</script><p>real</p>")
    assert "<script>" not in out
    assert "real" in out


def test_strip_html_collapses_runs_of_blank_lines() -> None:
    assert strip_html("<p>a</p><p></p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_no_html_tags_survive_into_the_prompt() -> None:
    item = WorkItem(
        id=1,
        type="Bug",
        title="t",
        description=strip_html("<div><p>Broken <code>charge()</code></p></div>"),
        acceptance_criteria=strip_html("<ul><li>works</li></ul>"),
    )
    text = build_prompt(item)
    assert "<" not in text.replace("<id>", "")
    assert "Broken charge()" in text


# ------------------------------------------------------------------ PR title/body/commit


def test_pr_title_matches_the_spec_shape() -> None:
    assert pr_title(ITEM) == "Bug #4123: Null reference when charging a zero-amount order"


def test_pr_description_has_the_required_sections_and_the_ab_link() -> None:
    body = pr_description(ITEM, summary="Fixed the null check.", files=["app.py"])
    assert "## Summary" in body
    assert "Fixed the null check." in body
    assert "## What changed" in body
    assert "- `app.py`" in body
    assert "## How to test" in body
    # docs/architecture.md §3 step 9: this exact line is what makes ADO link the work item.
    assert "Fixes AB#4123" in body


def test_pr_description_survives_an_empty_harness_summary() -> None:
    body = pr_description(ITEM, summary="   ", files=[])
    assert "did not produce a summary" in body
    assert "- (no files listed)" in body


def test_pr_description_caps_the_file_list() -> None:
    body = pr_description(ITEM, summary="s", files=[f"f{n}.py" for n in range(60)])
    assert "- `f49.py`" in body
    assert "- `f50.py`" not in body
    assert "... and 10 more" in body


def test_commit_message_is_title_then_summary() -> None:
    message = commit_message(ITEM, summary="Added a guard.")
    first, _, rest = message.partition("\n")
    assert first == pr_title(ITEM)
    assert rest.strip() == "Added a guard."
