"""The Invocations HTTP surface (docs/architecture.md §2, docs/contract.md)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ticket_to_pr.config import Settings
from ticket_to_pr.main import (
    PLATFORM_INVOCATION_HEADERS,
    PROTOCOL_VERSION,
    RUN_ID_RE,
    create_app,
)
from ticket_to_pr.models import ErrorCode

from .conftest import make_settings, request_body


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the background orchestration so HTTP tests do not do git work."""

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("ticket_to_pr.run.Orchestrator.execute", noop)


# ------------------------------------------------------------------------------- /healthz


def test_healthz_returns_200_once_config_validation_has_passed(client: TestClient) -> None:
    """docs/contract.md §1: 200 "once configuration validated at startup"."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "protocol": PROTOCOL_VERSION,
        "harness": "fake",
    }


def test_the_app_cannot_be_built_at_all_when_config_is_invalid() -> None:
    """There is no window in which /healthz answers 200 with a bad configuration."""
    from ticket_to_pr.config import ConfigError

    with pytest.raises(ConfigError):
        create_app(Settings.from_env({}))


def test_healthz_never_reports_a_token_or_the_allowlist(client: TestClient) -> None:
    body = client.get("/healthz").text
    assert "token" not in body.casefold()
    assert "contoso" not in body


# ----------------------------------------------------------------------------- /readiness


def test_readiness_returns_200_once_config_validation_has_passed(client: TestClient) -> None:
    """Hosted-agent runtime contract: "Return 200 OK from GET /readiness".

    The platform will not move the agent version to ``active`` without it.
    """
    response = client.get("/readiness")
    assert response.status_code == 200


def test_readiness_mirrors_healthz(client: TestClient) -> None:
    """One handler, two paths: the probes must never disagree about readiness."""
    healthz = client.get("/healthz")
    readiness = client.get("/readiness")
    assert readiness.status_code == healthz.status_code
    assert readiness.json() == healthz.json()


def test_the_app_cannot_serve_readiness_when_config_is_invalid() -> None:
    """The same guarantee as /healthz: no window in which a bad configuration is ready."""
    from ticket_to_pr.config import ConfigError

    with pytest.raises(ConfigError):
        create_app(Settings.from_env({}))


def test_readiness_never_reports_a_token_or_the_allowlist(client: TestClient) -> None:
    body = client.get("/readiness").text
    assert "token" not in body.casefold()
    assert "contoso" not in body


# ---------------------------------------------------------------------------- POST accepted


def test_post_invocations_returns_202_with_a_run_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stall(monkeypatch)
    response = client.post("/invocations", json=request_body())
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"runId", "status", "correlationId"}
    assert body["status"] == "queued"
    assert body["correlationId"] == "test-correlation"
    assert re.fullmatch(r"[0-9a-f-]{36}", body["runId"])
    assert response.headers["Location"] == f"/invocations/{body['runId']}"


def test_the_status_endpoint_echoes_the_correlation_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stall(monkeypatch)
    run_id = client.post("/invocations", json=request_body(correlationId="abc-123")).json()[
        "runId"
    ]
    status = client.get(f"/invocations/{run_id}").json()
    assert status["correlationId"] == "abc-123"
    assert status["runId"] == run_id
    assert status["status"] in {"queued", "running", "succeeded", "failed"}
    assert set(status) == {
        "runId", "status", "phase", "startedAt", "finishedAt", "result", "error",
        "correlationId",
    }


def test_an_unknown_run_id_is_404(client: TestClient) -> None:
    response = client.get("/invocations/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ------------------------------------------------------- validation errors are 400, not 422


@pytest.mark.parametrize(
    "body",
    [
        request_body(harness="claude-code"),
        request_body(model="gpt-6"),
        request_body(coder="codex"),
        request_body(branch="-rf; rm"),
        request_body(branch="bug fix"),
        request_body(organization="https://dev.azure.com/contoso"),
        request_body(version="2"),
        request_body(workItem={"type": "Bug", "id": 0}),
        request_body(workItem={"type": "Bug", "id": -1}),
        {},
        {"version": "1"},
    ],
)
def test_every_validation_failure_is_400_not_422(client: TestClient, body: Any) -> None:
    """docs/contract.md §3: unknown fields are rejected with 400. FastAPI's default is 422."""
    response = client.post("/invocations", json=body)
    assert response.status_code == 400, response.text


def test_snake_case_work_item_is_a_400(client: TestClient) -> None:
    """``populate_by_name=False``: ``work_item`` must not be smuggled past ``workItem``."""
    body = request_body()
    body["work_item"] = body.pop("workItem")
    response = client.post("/invocations", json=body)
    assert response.status_code == 400


def test_a_non_json_body_is_400(client: TestClient) -> None:
    response = client.post(
        "/invocations", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_a_json_array_body_is_400(client: TestClient) -> None:
    assert client.post("/invocations", json=[request_body()]).status_code == 400


def test_the_400_body_names_the_field_without_echoing_the_value(client: TestClient) -> None:
    response = client.post("/invocations", json=request_body(branch="bug fix; rm -rf /"))
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "bad_request"
    fields = {item["field"] for item in payload["error"]["details"]}
    assert "branch" in fields
    assert "rm -rf" not in response.text


def test_the_400_body_names_the_offending_unknown_field(client: TestClient) -> None:
    response = client.post("/invocations", json=request_body(harness="codex"))
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert "harness" in fields


# ----------------------------------------------------------------- the run reaches the store


def test_a_queued_run_is_immediately_pollable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stall(monkeypatch)
    run_id = client.post("/invocations", json=request_body()).json()["runId"]
    assert client.get(f"/invocations/{run_id}").status_code == 200


def test_two_invocations_get_distinct_run_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stall(monkeypatch)
    first = client.post("/invocations", json=request_body()).json()["runId"]
    second = client.post("/invocations", json=request_body(branch="feature/other")).json()[
        "runId"
    ]
    assert first != second


# ------------------------------------------------- the platform invocation id (run id)
#
# The Foundry gateway mints its own invocation id and validates the id format on
# ``GET /invocations/{id}``, rejecting anything that is not one of its own -- so this
# service adopts the gateway's id as its run id to make polling work at all. That makes an
# inbound *header* the key of a process-wide registry, and it reaches a ``Location``
# response header and INFO log lines on the way. It had no validation of any kind: a
# 5000-character value, a value carrying CRLF, and a repeat of an id already in use were
# all accepted whole, and the repeat silently overwrote the first caller's run.


def _post_with_id(client: TestClient, header_value: str, **body: object) -> Any:
    return client.post(
        "/invocations",
        json=request_body(**body),
        headers={"x-agent-invocation-id": header_value},
    )


def test_a_well_formed_platform_invocation_id_is_adopted_as_the_run_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason any of this exists: without adopting it, the gateway cannot poll."""
    _stall(monkeypatch)
    response = _post_with_id(client, "7f3a1c20-0000-4000-8000-abcdefabcdef")
    assert response.status_code == 202
    assert response.json()["runId"] == "7f3a1c20-0000-4000-8000-abcdefabcdef"
    assert client.get("/invocations/7f3a1c20-0000-4000-8000-abcdefabcdef").status_code == 200


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("crlf", "abc\r\nX-Injected: 1"),
        ("newline", "abc\ninjected"),
        ("path traversal", "../../../etc/passwd"),
        ("slash", "a/b"),
        ("too long", "a" * 5000),
        ("at the boundary", "a" * 129),
        ("space", "has a space"),
        ("null-ish unicode", "abc\u0000def"),
    ],
)
def test_a_malformed_platform_invocation_id_is_ignored_not_adopted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, label: str, value: str
) -> None:
    """Fall back to a generated UUID rather than 400.

    The *platform* sets this header, not the caller of the published contract, so a bad
    value is the hosting environment misbehaving. Failing the invocation would punish a
    caller who cannot fix it; the run proceeds and only gateway correlation is lost.
    """
    _stall(monkeypatch)
    response = _post_with_id(client, value)
    assert response.status_code == 202, label
    run_id = response.json()["runId"]
    assert run_id != value, label
    assert re.fullmatch(r"[0-9a-f-]{36}", run_id), label


def test_a_rejected_id_never_reaches_the_location_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Header injection, concretely.

    ``Location`` is built by interpolating the run id. A raw CRLF in it would let whoever
    controls the header append headers of their own to this response.
    """
    _stall(monkeypatch)
    response = _post_with_id(client, "abc\r\nX-Injected: 1")
    location = response.headers["Location"]
    assert "X-Injected" not in location
    assert "\r" not in location and "\n" not in location
    assert location == f"/invocations/{response.json()['runId']}"
    assert "x-injected" not in {k.casefold() for k in response.headers}


def test_a_repeated_invocation_id_does_not_overwrite_the_first_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /invocations/{id}`` has no ownership check and cannot have one.

    The platform authorizes the caller before the request arrives, so this process never
    learns who it was. Letting a second POST take a key already in use therefore hands the
    *first* caller the second caller's status, correlation id and error message on their
    next poll. The second run gets a fresh id instead; only its gateway correlation is lost.
    """
    _stall(monkeypatch)
    shared = "11111111-2222-4333-8444-555555555555"

    first = _post_with_id(client, shared, correlationId="first-caller")
    second = _post_with_id(client, shared, correlationId="second-caller", branch="feature/two")

    assert first.json()["runId"] == shared
    assert second.json()["runId"] != shared
    # The first caller's poll still answers with the first caller's run.
    assert client.get(f"/invocations/{shared}").json()["correlationId"] == "first-caller"
    second_id = second.json()["runId"]
    assert client.get(f"/invocations/{second_id}").json()["correlationId"] == "second-caller"


def test_every_recognised_platform_header_name_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLATFORM_INVOCATION_HEADERS had no coverage at all; a rename would go unnoticed."""
    _stall(monkeypatch)
    for index, name in enumerate(PLATFORM_INVOCATION_HEADERS):
        value = f"header-probe-{index}"
        response = client.post(
            "/invocations",
            json=request_body(branch=f"feature/probe-{index}"),
            headers={name: value},
        )
        assert response.json()["runId"] == value, name


def test_the_run_id_pattern_admits_both_id_shapes_actually_in_use() -> None:
    """A guid from the gateway and a local uuid4 must both pass, and little else."""
    import uuid

    assert RUN_ID_RE.match(str(uuid.uuid4()))
    assert RUN_ID_RE.match("7F3A1C20-0000-4000-8000-ABCDEFABCDEF")
    assert RUN_ID_RE.match("a" * 128)
    assert not RUN_ID_RE.match("a" * 129)
    assert not RUN_ID_RE.match("")
    # `\Z`, not `$`: `$` also matches just before a trailing newline, which would let a
    # value ending in CRLF through the check and into the Location header.
    assert not RUN_ID_RE.match("abcd\n")


def test_the_service_never_reads_the_authorization_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inbound authorization is the platform's job; this process must not depend on it."""
    _stall(monkeypatch)
    without = client.post("/invocations", json=request_body())
    with_header = client.post(
        "/invocations",
        json=request_body(branch="feature/other"),
        headers={"Authorization": "Bearer irrelevant"},
    )
    assert without.status_code == with_header.status_code == 202


def test_openapi_advertises_no_harness_model_or_coder_field(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["InvocationRequest"]
    assert set(request_schema["properties"]) == {
        "version", "organization", "project", "workItem", "repository", "branch",
        "baseBranch", "correlationId",
    }
    assert request_schema.get("additionalProperties") is False


def test_the_error_code_enum_in_the_schema_is_the_closed_v1_set(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    codes = set(schema["components"]["schemas"]["ErrorCode"]["enum"])
    assert codes == {code.value for code in ErrorCode}


def test_a_fake_harness_deployment_is_only_reachable_in_dev_mode() -> None:
    """create_app is the last line of defence, but the gate is at Settings.from_env."""
    from ticket_to_pr.config import ConfigError

    with pytest.raises(ConfigError, match="only available in dev mode"):
        Settings.from_env({"T2PR_ALLOWED_ORGS": "contoso", "T2PR_HARNESS": "fake"})


def test_a_settings_object_with_an_unknown_harness_cannot_build_an_app() -> None:
    with pytest.raises(Exception):  # noqa: B017 - ConfigError or ValueError, both are fatal
        create_app(make_settings(harness="aider"))  # type: ignore[arg-type]
