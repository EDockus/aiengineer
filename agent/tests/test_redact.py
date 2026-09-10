"""Redaction, with the Personal Access Token specifically in mind.

docs/deploy.md §3: Azure DevOps authentication uses a PAT, because the Foundry Entra
agent identity cannot be entitled in an organization (``VS403283``) and cannot federate
into one that can (``AADSTS700231``). That decision puts a long-lived secret inside the
process, so the machinery that keeps it out of pull request bodies, error payloads and
logs is load-bearing in a way it was not when every credential expired within the hour.

Two layers, and both are tested here:

* **Shape** -- an ``Authorization`` header in any scheme, inline URL credentials, a JWT.
  Catches a secret nobody registered, including one the agent never held.
* **Value** -- the PAT itself, registered at startup. A PAT is an undistinguished
  alphanumeric string, so shape alone would never find it in free text.
"""

from __future__ import annotations

import pytest

from ticket_to_pr.redact import (
    MIN_SECRET_LENGTH,
    PLACEHOLDER,
    redact,
    register_secret,
)

PAT = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst"


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """Keep registrations from leaking between tests."""
    from ticket_to_pr import redact as module

    saved = set(module._KNOWN_SECRETS)
    yield
    module._KNOWN_SECRETS.clear()
    module._KNOWN_SECRETS.update(saved)


# ----------------------------------------------------------------------------- by shape


@pytest.mark.parametrize(
    "scheme",
    ["Bearer", "bearer", "Basic", "basic", "BASIC", "Token", "negotiate"],
)
def test_an_authorization_header_is_redacted_in_any_scheme(scheme: str) -> None:
    """Not just Bearer.

    Azure DevOps takes the PAT as HTTP Basic, and ``git`` echoes the header it was handed
    into stderr when a fetch or push is refused -- which is exactly the text that ends up
    in a run log and an ``error.message``.
    """
    line = f"fatal: unable to access: Authorization: {scheme} {PAT} was rejected"
    out = redact(line)
    assert PAT not in out
    assert PLACEHOLDER in out


def test_inline_url_credentials_are_redacted() -> None:
    out = redact(f"remote: https://anything:{PAT}@dev.azure.com/contoso/_git/x")
    assert PAT not in out


def test_a_jwt_is_redacted() -> None:
    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payloadpayload.signature"
    assert jwt not in redact(f"token was {jwt} here")


# ----------------------------------------------------------------------------- by value


def test_the_pat_is_not_caught_by_shape_alone() -> None:
    """The reason value-based redaction exists at all.

    A PAT in running prose looks like any other word. If this ever starts passing without
    a registration, the shape rules have become broad enough to eat ordinary text.
    """
    assert PAT in redact(f"the harness printed {PAT} while cloning")


def test_a_registered_secret_is_redacted_anywhere_it_appears() -> None:
    register_secret(PAT)
    assert PAT not in redact(f"the harness printed {PAT} while cloning")
    assert PAT not in redact(f"{PAT}")
    assert PAT not in redact(f"prefix{PAT}suffix")


def test_registering_is_idempotent() -> None:
    register_secret(PAT)
    register_secret(PAT)
    assert redact(f"x {PAT} y") == f"x {PLACEHOLDER} y"


def test_short_values_are_not_registered() -> None:
    """A short "secret" is more likely a substring of prose; blanking it corrupts text."""
    register_secret("abc")
    assert redact("abc is the start of the alphabet") == "abc is the start of the alphabet"


def test_an_empty_secret_is_ignored() -> None:
    """Dev mode has no PAT, so register_secret('') must be a no-op, not a catch-all."""
    register_secret("")
    assert redact("nothing to see") == "nothing to see"


def test_the_length_floor_is_the_documented_one() -> None:
    register_secret("x" * (MIN_SECRET_LENGTH - 1))
    assert redact("x" * (MIN_SECRET_LENGTH - 1)) == "x" * (MIN_SECRET_LENGTH - 1)
    register_secret("y" * MIN_SECRET_LENGTH)
    assert redact("y" * MIN_SECRET_LENGTH) == PLACEHOLDER


# ------------------------------------------------------------------- the startup wiring


def test_create_app_registers_the_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring, not just the function.

    A registration that never happens is worth nothing, so this asserts the PAT is live in
    the redactor after the app is built the way production builds it.
    """
    from tests.conftest import make_settings
    from ticket_to_pr.main import create_app

    create_app(make_settings(ado_pat=PAT))
    assert PAT not in redact(f"a run log mentioning {PAT}")
