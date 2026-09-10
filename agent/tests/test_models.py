"""Request validation and the wire contract (docs/contract.md §3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ticket_to_pr.models import (
    ErrorCode,
    InvocationAccepted,
    InvocationRequest,
    Phase,
    RunState,
)

from .conftest import request_body


def _errors(exc: pytest.ExceptionInfo[ValidationError]) -> list[dict[str, object]]:
    return list(exc.value.errors())


def test_minimal_valid_request_parses() -> None:
    req = InvocationRequest.model_validate(request_body())
    assert req.work_item.id == 4123
    assert req.work_item.type == "Bug"
    assert req.base_branch == "main"
    assert req.correlation_id == "test-correlation"


def test_optional_fields_default_to_none() -> None:
    body = request_body()
    del body["baseBranch"]
    del body["correlationId"]
    req = InvocationRequest.model_validate(body)
    assert req.base_branch is None
    assert req.correlation_id is None


# ---------------------------------------------------------------------------- unknown fields


@pytest.mark.parametrize("field", ["harness", "model", "coder"])
def test_the_forbidden_override_fields_are_rejected(field: str) -> None:
    """docs/contract.md §3: there is no harness/model/coder field. Supplying one is an error."""
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(request_body(**{field: "claude-code"}))
    assert any(e["type"] == "extra_forbidden" for e in _errors(exc))


def test_arbitrary_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(request_body(somethingElse=1))
    assert any(e["type"] == "extra_forbidden" for e in _errors(exc))


def test_unknown_field_inside_work_item_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(
            request_body(workItem={"type": "Bug", "id": 4123, "model": "gpt-6"})
        )
    assert any(e["type"] == "extra_forbidden" for e in _errors(exc))


# ------------------------------------------------------------------- alias, not snake_case


@pytest.mark.parametrize(
    ("snake", "value"),
    [("work_item", {"type": "Bug", "id": 1}), ("base_branch", "main"),
     ("correlation_id", "x")],
)
def test_snake_case_spellings_cannot_be_smuggled_past_the_alias(
    snake: str, value: object
) -> None:
    """``populate_by_name=False``: the camelCase alias is the only accepted spelling."""
    body = request_body()
    camel = {"work_item": "workItem", "base_branch": "baseBranch",
             "correlation_id": "correlationId"}[snake]
    body.pop(camel, None)
    body[snake] = value
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(body)
    kinds = {e["type"] for e in _errors(exc)}
    assert "extra_forbidden" in kinds


def test_work_item_alias_is_the_only_way_to_supply_it() -> None:
    body = request_body()
    body["work_item"] = body.pop("workItem")
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(body)
    fields = {".".join(str(p) for p in e["loc"]) for e in _errors(exc)}
    assert "workItem" in fields  # missing required
    assert "work_item" in fields  # and the snake_case one is extra


# ---------------------------------------------------------------------------- field rules


def test_version_must_be_the_string_one() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(version="2"))
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(version=1))


@pytest.mark.parametrize("bad_id", [0, -1, -4123])
def test_work_item_id_must_be_positive(bad_id: int) -> None:
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(request_body(workItem={"type": "Bug", "id": bad_id}))
    assert any("greater_than" in str(e["type"]) for e in _errors(exc))


def test_work_item_id_must_not_be_a_float_or_arbitrary_string() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(workItem={"type": "Bug", "id": 4123.5}))
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(workItem={"type": "Bug", "id": "abc"}))


def test_work_item_type_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(workItem={"type": "", "id": 1}))


@pytest.mark.parametrize("missing", ["version", "organization", "project", "repository",
                                     "branch", "workItem"])
def test_required_fields_are_required(missing: str) -> None:
    body = request_body()
    del body[missing]
    with pytest.raises(ValidationError) as exc:
        InvocationRequest.model_validate(body)
    assert any(e["type"] == "missing" for e in _errors(exc))


@pytest.mark.parametrize(
    "organization",
    [
        "https://dev.azure.com/contoso",  # a URL, not a name (docs/contract.md §3)
        "contoso/Platform",
        "../contoso",
        "cont/oso\n",
        "",
        "-contoso",
        "a" * 65,
    ],
)
def test_organization_must_be_a_name_not_a_url_or_path(organization: str) -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(organization=organization))


def test_project_and_repository_use_the_same_name_rule() -> None:
    for field in ("project", "repository"):
        with pytest.raises(ValidationError):
            InvocationRequest.model_validate(request_body(**{field: "a/b"}))


# ---------------------------------------------------------------------- branch validation


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "bugfix/4123-null-ref",
        "release/1.2.3",
        "feature/some_thing",
        "a",
        "A.B-C/d_e",
    ],
)
def test_branch_names_matching_the_spec_regex_are_accepted(branch: str) -> None:
    assert InvocationRequest.model_validate(request_body(branch=branch)).branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "bug fix/1",        # space
        "bugfix/é",    # non-ascii
        "bug~fix",
        "bug^fix",
        "bug:fix",
        "bug?fix",
        "bug*fix",
        "bug[fix",
        "bug\\fix",
        "bug\tfix",
        "bug\nfix",
        "feat;rm -rf /",
        "$(whoami)",
        "-rf",
        "--upload-pack=touch /tmp/pwn",
        "",
    ],
)
def test_branch_names_failing_the_spec_regex_are_rejected(branch: str) -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(branch=branch))


@pytest.mark.parametrize(
    "branch",
    ["-leading-dash", "/leading-slash", "trailing/", "trailing.", "x.lock",
     "a..b", "a//b", "a@{b"],
)
def test_branch_shapes_git_itself_rejects_are_rejected(branch: str) -> None:
    """Beyond the spec regex: shapes git refuses, or that argv would read as an option."""
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(branch=branch))


def test_base_branch_uses_the_same_rules_as_branch() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(baseBranch="-rf"))
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(baseBranch="ma in"))


def test_base_branch_may_be_omitted_but_not_empty() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(baseBranch=""))


def test_branch_is_length_capped() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate(request_body(branch="a" * 201))


# ------------------------------------------------------------------------ response models


def test_error_code_set_is_exactly_the_eleven_v1_codes() -> None:
    """docs/contract.md §6: the set is closed. A new member here is a contract break."""
    assert {c.value for c in ErrorCode} == {
        "work_item_not_found",
        "repository_not_found",
        "branch_exists",
        "ado_forbidden",
        "clone_failed",
        "harness_failed",
        "harness_no_changes",
        "push_failed",
        "pr_failed",
        "timeout",
        "internal",
    }


def test_run_state_and_phase_match_the_spec() -> None:
    assert {s.value for s in RunState} == {"queued", "running", "succeeded", "failed"}
    assert {p.value for p in Phase} == {
        "fetching_work_item",
        "cloning",
        "coding",
        "pushing",
        "creating_pr",
        "done",
    }


def test_responses_serialise_with_camel_case_aliases() -> None:
    dumped = InvocationAccepted(
        run_id="abc", status=RunState.QUEUED, correlation_id="c"
    ).model_dump()
    assert dumped == {"runId": "abc", "status": "queued", "correlationId": "c"}
