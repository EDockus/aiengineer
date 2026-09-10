"""Pydantic request/response models -- the published v1 contract (docs/contract.md).

These models are the boundary of the service. Everything inward of them is typed
domain data; nothing untrusted crosses without passing through here first.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

# docs/contract.md §3: "Must match ^[A-Za-z0-9._/-]+$".
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# ADO org / project / repo names. Kept deliberately narrow: these values are
# interpolated into REST URLs, so anything path-ish or scheme-ish is rejected here
# rather than relied upon to be encoded later.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class ErrorCode(StrEnum):
    """The exhaustive v1 error code set (docs/contract.md §6). Do not extend."""

    WORK_ITEM_NOT_FOUND = "work_item_not_found"
    REPOSITORY_NOT_FOUND = "repository_not_found"
    BRANCH_EXISTS = "branch_exists"
    ADO_FORBIDDEN = "ado_forbidden"
    CLONE_FAILED = "clone_failed"
    HARNESS_FAILED = "harness_failed"
    HARNESS_NO_CHANGES = "harness_no_changes"
    PUSH_FAILED = "push_failed"
    PR_FAILED = "pr_failed"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Phase(StrEnum):
    FETCHING_WORK_ITEM = "fetching_work_item"
    CLONING = "cloning"
    CODING = "coding"
    PUSHING = "pushing"
    CREATING_PR = "creating_pr"
    DONE = "done"


class ContractModel(BaseModel):
    """Base for every wire model: camelCase on the wire, snake_case in Python.

    ``extra="forbid"`` implements docs/contract.md §3's "unknown fields are
    rejected with 400", and
    leaving ``populate_by_name`` off means the camelCase alias is the *only*
    accepted spelling -- a caller cannot smuggle ``work_item`` past the alias.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=False,
        extra="forbid",
        str_strip_whitespace=True,
    )


class WorkItemRef(ContractModel):
    type: Annotated[str, Field(min_length=1, max_length=128)]
    id: Annotated[int, Field(gt=0)]


class InvocationRequest(ContractModel):
    """POST /invocations body.

    There is deliberately no ``harness``, ``model`` or ``coder`` field: those are
    deploy-time configuration only (docs/contract.md §3). ``extra="forbid"`` makes an
    attempt to supply one a 400 rather than a silently ignored field.
    """

    version: Literal["1"]
    organization: Annotated[str, Field(min_length=1, max_length=64)]
    project: Annotated[str, Field(min_length=1, max_length=64)]
    work_item: WorkItemRef
    repository: Annotated[str, Field(min_length=1, max_length=64)]
    branch: Annotated[str, Field(min_length=1, max_length=200)]
    base_branch: Annotated[str | None, Field(default=None, max_length=200)] = None
    correlation_id: Annotated[str | None, Field(default=None, max_length=200)] = None

    @field_validator("organization", "project", "repository")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                "must be an Azure DevOps name (letters, digits, space, dot, underscore, "
                "hyphen), not a URL"
            )
        return v

    @field_validator("branch", "base_branch")
    @classmethod
    def _check_branch(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not BRANCH_RE.match(v):
            raise ValueError("must match ^[A-Za-z0-9._/-]+$")
        # Beyond the published regex, reject shapes git itself rejects or that would be
        # ambiguous on a command line. A leading "-" in particular would otherwise be
        # parsed as an option by git.
        if v.startswith(("-", "/")) or v.endswith(("/", ".lock", ".")):
            raise ValueError("must not start with '-' or '/', or end with '/', '.' or '.lock'")
        if ".." in v or "//" in v or "@{" in v:
            raise ValueError("must not contain '..', '//' or '@{'")
        return v


class ResponseModel(BaseModel):
    """Base for models the service *emits*.

    Same camelCase wire shape as :class:`ContractModel`, but constructible from Python
    with snake_case keywords, because these are built in code rather than parsed from an
    untrusted body. FastAPI serialises them by alias.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )


class InvocationAccepted(ResponseModel):
    """202 body returned immediately by POST /invocations."""

    run_id: str
    status: RunState
    correlation_id: str | None = None


class RunResult(ResponseModel):
    pull_request_id: int
    pull_request_url: str
    branch: str
    commits: int
    files_changed: int


class RunError(ResponseModel):
    code: ErrorCode
    message: str


class RunStatus(ResponseModel):
    """GET /invocations/{runId} body."""

    run_id: str
    status: RunState
    phase: Phase
    started_at: datetime
    finished_at: datetime | None = None
    result: RunResult | None = None
    error: RunError | None = None
    correlation_id: str | None = None


class WorkItem(BaseModel):
    """Domain model for a fetched ADO work item (not part of the wire contract)."""

    model_config = ConfigDict(frozen=True)

    id: int
    type: str
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    repro_steps: str = ""
    tags: tuple[str, ...] = ()
    linked_titles: tuple[str, ...] = ()


class Repository(BaseModel):
    """Domain model for a resolved ADO git repository."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    remote_url: str
    default_branch: str
    web_url: str


class RunFailure(Exception):
    """A failure already mapped to a v1 contract error code, raised where it occurs.

    Every layer raises this instead of a bare exception, so ``run.py`` never has to
    guess a code and no generic handler can flatten a specific failure into
    ``internal`` (docs/architecture.md §5; the closed code set is docs/contract.md §6).

    ``ambiguous`` means the call failed on this side of the wire -- a connect or read
    timeout, a dropped connection -- so whether the server carried out the operation is
    unknown. A definite HTTP rejection is not ambiguous. The post-push cleanup in
    :mod:`ticket_to_pr.run` uses this to decide whether it must first check that it is not
    about to delete the source branch of a pull request that really did get created.
    """

    def __init__(self, code: ErrorCode, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.ambiguous = ambiguous
