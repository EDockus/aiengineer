"""Startup configuration validation and the dev-mode fail-closed gate
(docs/configuration.md §1 and §3).

The dev-mode tests are deliberately adversarial: dev mode disables Entra authentication
for Azure DevOps, so the interesting question is not "do the documented 16 cases pass"
but "is there *any* combination that opens the gate when it should not". Every test below
starts from a fully-valid dev environment and breaks exactly one condition.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from ticket_to_pr import config as config_module
from ticket_to_pr.config import (
    CLOUD_ENV_MARKERS,
    DEV_MODE_PHRASE,
    AuthMode,
    ConfigError,
    HarnessName,
    Settings,
)


def prod_env(**overrides: str) -> dict[str, str]:
    # T2PR_ADO_PAT is required outside dev mode (docs/deploy.md §3), so a "valid
    # production environment" now includes one. Tests that assert it is *missing* pass
    # T2PR_ADO_PAT="" explicitly.
    env = {"T2PR_ALLOWED_ORGS": "contoso", "T2PR_ADO_PAT": "fake-pat-for-tests"}
    env.update(overrides)
    return env


def dev_env(**overrides: str) -> dict[str, str]:
    """An environment that *should* open the dev-mode gate."""
    env = {
        "T2PR_ALLOWED_ORGS": "contoso",
        "T2PR_DEV_MODE": DEV_MODE_PHRASE,
        "T2PR_ADO_BASE_URL": "http://127.0.0.1:8080",
        "T2PR_HARNESS": "fake",
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def _no_production_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the production marker at a path that does not exist, unless a test creates it."""
    monkeypatch.setattr(config_module, "PRODUCTION_MARKER", tmp_path / "no-such-marker")


# ---------------------------------------------------------------------------- happy paths


def test_defaults_match_the_spec_table() -> None:
    cfg = Settings.from_env(prod_env())
    assert cfg.harness is HarnessName.CODEX
    assert cfg.model_deployment == "gpt-6"
    assert cfg.max_run_minutes == 20
    assert cfg.max_run_seconds == 1200
    assert cfg.max_files_changed == 50
    assert cfg.pr_draft is True
    assert cfg.pr_reviewers == ()
    assert cfg.commit_author_name == "Ticket-to-PR agent"
    assert cfg.commit_author_email == "ticket-to-pr@noreply.local"
    assert cfg.log_level == "INFO"
    assert cfg.ado_base_url == "https://dev.azure.com"
    assert cfg.auth_mode is AuthMode.PAT
    assert cfg.dev_mode is False


def test_dev_environment_opens_the_gate() -> None:
    cfg = Settings.from_env(dev_env())
    assert cfg.dev_mode is True
    assert cfg.auth_mode is AuthMode.DEV_STATIC
    assert cfg.harness is HarnessName.FAKE
    assert cfg.host == "127.0.0.1"


# ------------------------------------------------------------------------- allowlist rules


def test_missing_allowed_orgs_is_a_startup_error() -> None:
    with pytest.raises(ConfigError, match="T2PR_ALLOWED_ORGS"):
        Settings.from_env({})


@pytest.mark.parametrize("value", ["", "   ", ",", " , , "])
def test_empty_allowed_orgs_rejects_everything_at_startup(value: str) -> None:
    """docs/configuration.md §1: "An empty allowlist rejects every request"."""
    with pytest.raises(ConfigError, match="rejects every request"):
        Settings.from_env({"T2PR_ALLOWED_ORGS": value})


def test_org_allowlist_is_case_insensitive_and_trimmed() -> None:
    cfg = Settings.from_env(prod_env(T2PR_ALLOWED_ORGS=" Contoso , FABRIKAM "))
    assert cfg.org_allowed("contoso")
    assert cfg.org_allowed("CONTOSO")
    assert cfg.org_allowed("fabrikam")
    assert not cfg.org_allowed("northwind")
    assert not cfg.org_allowed("contoso2")


def test_org_url_is_built_from_the_base_url() -> None:
    cfg = Settings.from_env(prod_env())
    assert cfg.org_url("contoso") == "https://dev.azure.com/contoso"


# ----------------------------------------------------------------------- per-variable rules


@pytest.mark.parametrize(
    ("name", "value", "needle"),
    [
        ("T2PR_MAX_RUN_MINUTES", "abc", "not an integer"),
        ("T2PR_MAX_RUN_MINUTES", "0", "outside the allowed range"),
        ("T2PR_MAX_RUN_MINUTES", "-5", "outside the allowed range"),
        ("T2PR_MAX_RUN_MINUTES", "1000", "outside the allowed range"),
        ("T2PR_MAX_FILES_CHANGED", "0", "outside the allowed range"),
        ("T2PR_MAX_FILES_CHANGED", "nope", "not an integer"),
        ("T2PR_PORT", "0", "outside the allowed range"),
        ("T2PR_PORT", "70000", "outside the allowed range"),
        ("T2PR_PR_DRAFT", "maybe", "not a boolean"),
        ("T2PR_LOG_LEVEL", "chatty", "not one of"),
        ("T2PR_COMMIT_AUTHOR_EMAIL", "not-an-email", "not an email address"),
        ("T2PR_COMMIT_AUTHOR_EMAIL", "a b@c.d", "not an email address"),
        ("T2PR_HARNESS", "gpt-6", "not one of"),
        ("T2PR_HARNESS", "aider", "not one of"),
    ],
)
def test_bad_values_fail_fast_with_a_clear_message(name: str, value: str, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        Settings.from_env(prod_env(**{name: value}))


def test_blank_model_deployment_falls_back_to_the_default_rather_than_empty() -> None:
    # ``_str`` treats whitespace as absent, so the documented default applies.
    assert Settings.from_env(prod_env(T2PR_MODEL_DEPLOYMENT="   ")).model_deployment == "gpt-6"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_boolean_true_spellings(value: str) -> None:
    assert Settings.from_env(prod_env(T2PR_PR_DRAFT=value)).pr_draft is True


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF"])
def test_boolean_false_spellings(value: str) -> None:
    assert Settings.from_env(prod_env(T2PR_PR_DRAFT=value)).pr_draft is False


def test_reviewers_are_split_and_trimmed() -> None:
    cfg = Settings.from_env(prod_env(T2PR_PR_REVIEWERS=" a-id , b-id ,"))
    assert cfg.pr_reviewers == ("a-id", "b-id")


def test_harness_names_are_case_insensitive() -> None:
    assert Settings.from_env(prod_env(T2PR_HARNESS="CODEX")).harness is HarnessName.CODEX
    assert (
        Settings.from_env(prod_env(T2PR_HARNESS="Claude-Code")).harness
        is HarnessName.CLAUDE_CODE
    )


# -------------------------------------------------------------- T2PR_HARNESS=fake is gated


def test_fake_harness_is_rejected_at_startup_when_dev_mode_is_off() -> None:
    """A production deployment must never be able to select the canned-change harness."""
    with pytest.raises(ConfigError, match="only available in dev mode"):
        Settings.from_env(prod_env(T2PR_HARNESS="fake"))


@pytest.mark.parametrize("spelling", ["fake", "FAKE", "Fake", " fake "])
def test_no_spelling_of_fake_slips_past_in_production(spelling: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(prod_env(T2PR_HARNESS=spelling))


def test_fake_is_not_advertised_in_the_production_error_message() -> None:
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(prod_env(T2PR_HARNESS="nonsense"))
    assert "fake" not in str(exc.value)


# ------------------------------------------------------------------ the dev-mode gauntlet


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "please", DEV_MODE_PHRASE.upper(),
                                   DEV_MODE_PHRASE + "!", "i-understand-this-disables-auth "
                                   "extra"])
def test_only_the_exact_phrase_enables_dev_mode(value: str) -> None:
    """Anything but the exact phrase is a hard error, never a silent "off"."""
    with pytest.raises(ConfigError, match="confirmation phrase"):
        Settings.from_env(dev_env(T2PR_DEV_MODE=value))


def test_whitespace_around_the_phrase_is_tolerated() -> None:
    assert Settings.from_env(dev_env(T2PR_DEV_MODE=f"  {DEV_MODE_PHRASE}  ")).dev_mode


def test_unset_dev_mode_means_off_not_error() -> None:
    cfg = Settings.from_env(prod_env(T2PR_DEV_MODE=""))
    assert cfg.dev_mode is False


def test_production_marker_file_closes_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "etc" / "production"
    marker.parent.mkdir(parents=True)
    marker.write_text("deployed", encoding="utf-8")
    monkeypatch.setattr(config_module, "PRODUCTION_MARKER", marker)
    with pytest.raises(ConfigError, match="deployed container image"):
        Settings.from_env(dev_env())


@pytest.mark.parametrize("marker", CLOUD_ENV_MARKERS)
def test_each_cloud_marker_alone_closes_the_gate(marker: str) -> None:
    with pytest.raises(ConfigError, match="cloud"):
        Settings.from_env(dev_env(**{marker: "set"}))


@pytest.mark.parametrize("marker", CLOUD_ENV_MARKERS)
def test_a_blank_cloud_marker_does_not_falsely_close_the_gate(marker: str) -> None:
    # An empty string is "not set"; only a real value means "we are in the cloud".
    assert Settings.from_env(dev_env(**{marker: "   "})).dev_mode is True


def test_two_cloud_markers_are_both_named_in_the_message() -> None:
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(dev_env(CONTAINER_APP_NAME="a", WEBSITE_SITE_NAME="b"))
    assert "CONTAINER_APP_NAME" in str(exc.value)
    assert "WEBSITE_SITE_NAME" in str(exc.value)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "10.0.0.5", "192.168.1.4", "example.com", "127.0.0.1.evil.com",
     "0.0.0.0 ", "localhost.evil.test", "[::]"],
)
def test_a_non_loopback_bind_closes_the_gate(host: str) -> None:
    with pytest.raises(ConfigError, match="loopback bind address"):
        Settings.from_env(dev_env(T2PR_HOST=host))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"])
def test_loopback_binds_are_accepted(host: str) -> None:
    assert Settings.from_env(dev_env(T2PR_HOST=host)).dev_mode is True


def test_dev_mode_without_an_ado_base_url_closes_the_gate() -> None:
    env = dev_env()
    del env["T2PR_ADO_BASE_URL"]
    with pytest.raises(ConfigError, match="T2PR_ADO_BASE_URL"):
        Settings.from_env(env)


@pytest.mark.parametrize(
    "url",
    [
        "https://dev.azure.com",
        "https://dev.azure.com/contoso",
        "http://10.0.0.5:8080",
        "http://evil.test",
        "http://127.0.0.1.evil.test:8080",
        "http://localhost.evil.test",
        "file:///etc/passwd",
        "ftp://127.0.0.1",
        "http://user@evil.test",
        "//127.0.0.1:8080",
        "127.0.0.1:8080",
    ],
)
def test_a_non_loopback_ado_base_url_closes_the_gate(url: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(dev_env(T2PR_ADO_BASE_URL=url))


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080", "http://localhost:8080", "https://127.0.0.1:9",
     "http://[::1]:8080", "http://127.0.0.1:8080/"],
)
def test_loopback_ado_base_urls_are_accepted(url: str) -> None:
    assert Settings.from_env(dev_env(T2PR_ADO_BASE_URL=url)).dev_mode is True


def test_no_single_broken_condition_can_be_compensated_by_the_others() -> None:
    """Search the whole 2**4 space of "one or more conditions broken"."""
    breakages: dict[str, dict[str, str]] = {
        "phrase": {"T2PR_DEV_MODE": "1"},
        "cloud": {"FOUNDRY_PROJECT_ENDPOINT": "https://x.services.ai.azure.com"},
        "bind": {"T2PR_HOST": "0.0.0.0"},
        "base_url": {"T2PR_ADO_BASE_URL": "https://dev.azure.com"},
    }
    names = list(breakages)
    for size in range(1, len(names) + 1):
        for combo in itertools.combinations(names, size):
            env = dev_env()
            for name in combo:
                env.update(breakages[name])
            with pytest.raises(ConfigError):
                Settings.from_env(env)


# ------------------------------------------------------- dev knobs are refused in production


def test_ado_base_url_override_is_refused_without_dev_mode() -> None:
    with pytest.raises(ConfigError, match="only permitted in dev mode"):
        Settings.from_env(prod_env(T2PR_ADO_BASE_URL="http://127.0.0.1:8080"))


def test_the_default_ado_base_url_is_fine_without_dev_mode() -> None:
    cfg = Settings.from_env(prod_env(T2PR_ADO_BASE_URL="https://dev.azure.com/"))
    assert cfg.ado_base_url == "https://dev.azure.com"


@pytest.mark.parametrize("name", ["T2PR_DEV_ADO_TOKEN", "T2PR_DEV_ALLOW_UNAUTHENTICATED"])
def test_half_configured_dev_auth_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="Refusing to start"):
        Settings.from_env(prod_env(**{name: "value"}))


def test_production_defaults_bind_all_interfaces() -> None:
    assert Settings.from_env(prod_env()).host == "0.0.0.0"


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises((AttributeError, TypeError)):
        settings.dev_mode = False  # type: ignore[misc]


def test_from_env_reads_os_environ_when_no_mapping_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("T2PR_ADO_BASE_URL", "T2PR_DEV_MODE", "T2PR_HARNESS", "T2PR_HOST"):
        monkeypatch.delenv(name, raising=False)
    for marker in CLOUD_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("T2PR_ALLOWED_ORGS", "contoso")
    monkeypatch.setenv("T2PR_ADO_PAT", "fake-pat-for-tests")
    assert Settings.from_env().allowed_orgs == frozenset({"contoso"})


def test_config_error_is_not_a_generic_exception_swallower() -> None:
    assert issubclass(ConfigError, RuntimeError)


def test_settings_never_holds_a_token_field() -> None:
    """SECURITY.md "Credentials": the Azure DevOps PAT is the only credential in this
    design, so no api-key, token, secret or password field belongs on Settings.
    """
    fields: Any = Settings.__dataclass_fields__
    for name in fields:
        assert not any(word in name for word in ("token", "secret", "key", "password"))
