"""Harness protocol, factory and the environment allowlist (docs/architecture.md §4.2).

The load-bearing test in this file is :func:`test_no_ado_token_or_t2pr_setting_reaches_a
_harness`: docs/architecture.md §4.2 says the harness gets repo files and model access and
nothing else, and the only way to know that is to build the environment and look inside it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ticket_to_pr.config import ADO_SCOPE, ConfigError, HarnessName, Settings
from ticket_to_pr.harness import (
    ENV_ALLOWLIST,
    ENV_SET_BY_AGENT,
    FORBIDDEN_ENV_FRAGMENTS,
    Harness,
    HarnessResult,
    clean_env,
    foundry_endpoint,
    get_harness,
    require_cli,
    run_cli,
)
from ticket_to_pr.harness.claude_code import ClaudeCodeHarness, _parse_output
from ticket_to_pr.harness.codex import TOKEN_ENV, CodexHarness
from ticket_to_pr.harness.fake import MARKER_FILE, FakeHarness
from ticket_to_pr.models import ErrorCode, RunFailure

DUMP_ENV = "import os, json, sys; sys.stdout.write(json.dumps(dict(os.environ)))"

# ---------------------------------------------------------------------------- the factory


def test_factory_returns_each_declared_harness() -> None:
    assert isinstance(get_harness("codex"), CodexHarness)
    assert isinstance(get_harness("claude-code"), ClaudeCodeHarness)
    assert isinstance(get_harness("fake"), FakeHarness)


def test_factory_accepts_the_enum_as_well_as_the_string() -> None:
    assert isinstance(get_harness(HarnessName.CODEX), CodexHarness)


@pytest.mark.parametrize(
    "name", ["", "gpt-6", "aider", "Codex", "CLAUDE-CODE", "codex ", "cursor", "none"]
)
def test_factory_raises_for_unknown_values(name: str) -> None:
    """docs/architecture.md §4.2: the factory raises at startup for unknown values."""
    with pytest.raises(ConfigError, match="unknown harness"):
        get_harness(name)


def test_factory_error_lists_the_permitted_values() -> None:
    with pytest.raises(ConfigError) as exc:
        get_harness("aider")
    assert "codex" in str(exc.value)
    assert "claude-code" in str(exc.value)


def test_every_harness_satisfies_the_protocol() -> None:
    for name in ("codex", "claude-code", "fake"):
        harness = get_harness(name)
        assert isinstance(harness, Harness)
        assert isinstance(harness.name, str) and harness.name


def test_harness_names_cover_the_enum() -> None:
    for member in HarnessName:
        assert get_harness(member).name == member.value


# ------------------------------------------------------------------------ the fake harness


def test_fake_harness_writes_exactly_one_file(tmp_path: Path, settings: Settings) -> None:
    harness = FakeHarness()
    harness.preflight(settings)
    result = harness.run(tmp_path, "the prompt", 60)
    assert result.exit_code == 0
    assert result.summary
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_FILE]


def test_fake_harness_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = FakeHarness().run(tmp_path / "one", "same prompt", 60)
    second = FakeHarness().run(tmp_path / "two", "same prompt", 60)
    assert first.summary == second.summary
    different = FakeHarness().run(tmp_path / "one", "other prompt", 60)
    assert different.summary != first.summary


def test_fake_harness_never_touches_the_network_or_a_model(tmp_path: Path) -> None:
    # It has no client, no endpoint and no credential of any kind.
    harness = FakeHarness()
    assert not hasattr(harness, "_client")
    result = harness.run(tmp_path, "p", 1)
    assert "fake" in result.summary.casefold()


# ------------------------------------------------------------- the environment allowlist


LEAKY_ENVIRONMENT = {
    "T2PR_ALLOWED_ORGS": "contoso",
    "T2PR_HARNESS": "fake",
    "T2PR_DEV_MODE": "i-understand-this-disables-auth",
    "T2PR_ADO_BASE_URL": "http://127.0.0.1:8080",
    "T2PR_MODEL_DEPLOYMENT": "gpt-6",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "http.extraheader",
    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.secret",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": "store",
    "GIT_ASKPASS": "/bin/echo",
    "AZURE_DEVOPS_EXT_PAT": "a-pat-that-must-not-escape",
    "SYSTEM_ACCESSTOKEN": "another-ado-token",
    "AWS_SECRET_ACCESS_KEY": "nope",
    "ANTHROPIC_API_KEY": "sk-should-not-be-here",
    "OPENAI_API_KEY": "sk-should-not-be-here-either",
    "AZURE_CLIENT_SECRET": "client-secret",
    # The managed-identity plumbing. None of these is *itself* an Azure DevOps token,
    # which is why an earlier allowlist waved them through -- but any one of these
    # groups lets its holder ask Entra for a token with the Azure DevOps audience, so
    # they are exactly as dangerous as the token they can be exchanged for.
    "IDENTITY_ENDPOINT": "http://169.254.169.254/msi/token",
    "IDENTITY_HEADER": "identity-header-that-mints-tokens",
    "IMDS_ENDPOINT": "http://169.254.169.254",
    "MSI_ENDPOINT": "http://169.254.169.254/metadata/identity/oauth2/token",
    "MSI_SECRET": "msi-secret-that-mints-tokens",
    "AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token",
    "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_TENANT_ID": "22222222-2222-2222-2222-222222222222",
    "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
}


@pytest.fixture
def leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LEAKY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("leaky_env")
def test_no_ado_token_or_t2pr_setting_reaches_a_harness() -> None:
    """docs/architecture.md §4.2, checked by building the env and inspecting it."""
    env = clean_env()
    for name in env:
        assert not name.startswith("T2PR_"), f"{name} leaked into the harness environment"
        assert not name.startswith("GIT_CONFIG_"), f"{name} leaked into the harness environment"
    assert "GIT_ASKPASS" not in env
    assert "AZURE_DEVOPS_EXT_PAT" not in env
    assert "SYSTEM_ACCESSTOKEN" not in env
    assert "AZURE_CLIENT_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    joined = "\n".join(f"{k}={v}" for k, v in env.items())
    for secret in ("a-pat-that-must-not-escape", "another-ado-token", "sk-should-not-be-here",
                   "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.secret", "client-secret"):
        assert secret not in joined


@pytest.mark.usefixtures("leaky_env")
def test_the_environment_is_an_allowlist_not_a_denylist() -> None:
    """A newly-invented variable is dropped by default, without anyone updating a list."""
    os.environ["SOME_BRAND_NEW_SECRET_2027"] = "value"
    try:
        env = clean_env()
        assert "SOME_BRAND_NEW_SECRET_2027" not in env
        assert set(env) <= set(ENV_ALLOWLIST) | set(ENV_SET_BY_AGENT)
    finally:
        del os.environ["SOME_BRAND_NEW_SECRET_2027"]


@pytest.mark.usefixtures("leaky_env")
def test_the_harness_cannot_obtain_a_token_for_azure_devops() -> None:
    """docs/architecture.md §6, stated as the property that actually matters.

    Not "no variable is called something DevOps-ish" -- that name-shape check passed
    happily while ``IDENTITY_ENDPOINT`` and ``IDENTITY_HEADER`` sat in the allowlist, and
    those two are enough to ask the managed identity for a token with *any* audience,
    including Azure DevOps' ``499b84ac-1321-427f-aa17-267ca6975798``. The property is that
    the harness environment contains no means, direct or indirect, of getting one.
    """
    env = clean_env()

    # Direct: no ADO token, and nothing carrying the ADO resource id.
    joined = "\n".join(f"{k}={v}" for k, v in env.items())
    assert ADO_SCOPE.split("/")[0] not in joined

    # Indirect, IMDS / App Service / Container Apps: the endpoint plus its header or
    # secret is a token-minting oracle for any resource the caller names.
    assert not {"IDENTITY_ENDPOINT", "MSI_ENDPOINT", "IMDS_ENDPOINT"} & set(env)
    assert not {"IDENTITY_HEADER", "MSI_SECRET"} & set(env)

    # Indirect, workload identity federation: the projected token file exchanged with the
    # client and tenant ids is an independent path to the same result.
    assert "AZURE_FEDERATED_TOKEN_FILE" not in env
    assert not {"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST"} & set(env)

    # And nothing at all that the identity plumbing could hide behind.
    for name in env:
        assert not any(fragment in name.upper() for fragment in FORBIDDEN_ENV_FRAGMENTS), (
            f"{name} could be a step towards a token"
        )


def test_the_allowlist_itself_names_nothing_credential_shaped() -> None:
    """Fails the build if the identity plumbing is ever put back."""
    for name in ENV_ALLOWLIST:
        assert not any(fragment in name.upper() for fragment in FORBIDDEN_ENV_FRAGMENTS)
        assert name.upper() != "PAT"
        assert not name.startswith("T2PR_")


@pytest.mark.usefixtures("leaky_env")
def test_the_servers_home_never_reaches_the_harness() -> None:
    """The harness must not be able to write where the server's interpreters read.

    Inheriting $HOME let a harness plant ``~/.local/lib/python*/site-packages/
    usercustomize.py`` or ``~/.profile``; the container's HEALTHCHECK and any operator
    login shell would then execute it, holding the unscrubbed configuration environment
    that ``launch.py`` deliberately keeps out of the server's own environ. Asserting
    membership of ENV_ALLOWLIST is not enough -- HOME is legitimately *set* by
    clean_env -- so this pins the value.
    """
    os.environ["HOME"] = "/work"
    env = clean_env()
    assert env["HOME"] != os.environ["HOME"]
    assert env["HOME"] != "/work"
    # And the per-run directory a real harness passes still wins.
    assert clean_env({"HOME": "/run/t2pr-xyz/repo"})["HOME"] == "/run/t2pr-xyz/repo"


def test_the_harness_ignores_user_site_packages() -> None:
    """PYTHONNOUSERSITE stops a usercustomize.py surviving from an earlier run."""
    assert clean_env()["PYTHONNOUSERSITE"] == "1"


def test_clean_env_supplies_safe_defaults_and_extras() -> None:
    env = clean_env({"CUSTOM": "x"})
    assert env["CUSTOM"] == "x"
    assert env["PATH"]
    assert env["HOME"]
    assert env["DO_NOT_TRACK"] == "1"


@pytest.mark.usefixtures("leaky_env")
def test_clean_env_extras_cannot_be_overridden_by_the_ambient_environment() -> None:
    env = clean_env({"HOME": "/work/repo"})
    assert env["HOME"] == "/work/repo"


def test_the_anthropic_base_url_is_the_account_not_the_project() -> None:
    """Cost one failed deployment; pinned so it cannot regress.

    Anthropic documents ``https://{resource}.services.ai.azure.com/anthropic`` -- the
    account host with no project path. Appending ``/anthropic`` to the project endpoint
    reaches Foundry's generic project API instead and fails with
    ``400 Missing required query parameter: api-version``, an error about a parameter the
    correct route never takes.
    """
    from ticket_to_pr.harness.claude_code import _anthropic_base_url

    project = "https://my-res.services.ai.azure.com/api/projects/core"
    assert _anthropic_base_url(project) == "https://my-res.services.ai.azure.com/anthropic"
    assert "/api/projects" not in _anthropic_base_url(project)
    assert _anthropic_base_url(project + "/") == "https://my-res.services.ai.azure.com/anthropic"


def test_the_foundry_scope_is_the_ai_azure_audience() -> None:
    """Also cost a deployment: cognitiveservices.azure.com is rejected by the model endpoint.

    The 401 names the audience it wanted -- ``audience is incorrect
    (https://ai.azure.com)`` -- and both strings look equally plausible beforehand.
    """
    from ticket_to_pr.harness import FOUNDRY_SCOPE

    assert FOUNDRY_SCOPE == "https://ai.azure.com/.default"


# ---------------------------------------------------------------- run_cli and its mapping


def test_run_cli_puts_the_prompt_on_stdin_not_argv(tmp_path: Path) -> None:
    """Work-item text is untrusted and argv is world-readable."""
    script = "import sys; data=sys.stdin.read(); sys.stdout.write(data.upper())"
    proc = run_cli(
        [sys.executable, "-c", script],
        repo_dir=tmp_path,
        prompt="secret work item text",
        timeout_s=30,
        env=clean_env(),
    )
    assert proc.stdout == "SECRET WORK ITEM TEXT"
    assert proc.returncode == 0


def test_run_cli_runs_in_the_repo_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    proc = run_cli(
        [sys.executable, "-c", "import os,sys; sys.stdout.write(os.getcwd())"],
        repo_dir=repo,
        prompt="",
        timeout_s=30,
        env=clean_env(),
    )
    assert Path(proc.stdout).resolve() == repo.resolve()


def test_run_cli_maps_a_real_timeout_to_the_timeout_error_code(tmp_path: Path) -> None:
    """The budget is enforced, not merely configured."""
    with pytest.raises(RunFailure) as exc:
        run_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            repo_dir=tmp_path,
            prompt="",
            timeout_s=1,
            env=clean_env(),
        )
    assert exc.value.code is ErrorCode.TIMEOUT
    assert "1s budget" in exc.value.message


def test_a_timed_out_harness_leaves_no_live_descendants(tmp_path: Path) -> None:
    """The timeout must reach the whole process tree, not just the CLI itself.

    Both harness CLIs spawn children of their own -- test runners, build tools, language
    servers. ``subprocess`` only ever signals the direct child, so before ``run_cli`` put
    the CLI in its own session a timed-out run left grandchildren alive in the container,
    still writing into the working tree that ``run.py`` was about to delete.
    """
    pid_file = tmp_path / "grandchild.pid"
    grandchild = (
        f"import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        f"time.sleep(60)"
    )
    script = (
        f"import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"time.sleep(60)"
    )
    with pytest.raises(RunFailure) as exc:
        run_cli(
            [sys.executable, "-c", script],
            repo_dir=tmp_path,
            prompt="",
            timeout_s=2,
            env=clean_env(),
        )
    assert exc.value.code is ErrorCode.TIMEOUT

    pid = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    pytest.fail(f"grandchild {pid} outlived the timed-out harness")


def test_run_cli_maps_a_missing_executable_to_harness_failed(tmp_path: Path) -> None:
    with pytest.raises(RunFailure) as exc:
        run_cli(
            ["/nonexistent/definitely-not-a-cli"],
            repo_dir=tmp_path,
            prompt="",
            timeout_s=5,
            env=clean_env(),
        )
    assert exc.value.code is ErrorCode.HARNESS_FAILED


def test_run_cli_returns_a_nonzero_exit_rather_than_raising(tmp_path: Path) -> None:
    proc = run_cli(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        repo_dir=tmp_path,
        prompt="",
        timeout_s=30,
        env=clean_env(),
    )
    assert proc.returncode == 3


def test_run_cli_passes_only_the_given_environment(tmp_path: Path) -> None:
    proc = run_cli(
        [sys.executable, "-c", DUMP_ENV],
        repo_dir=tmp_path,
        prompt="",
        timeout_s=30,
        env=clean_env({"MARKER": "yes"}),
    )
    import json

    child = json.loads(proc.stdout)
    assert child["MARKER"] == "yes"
    assert not [k for k in child if k.startswith("T2PR_")]


# ---------------------------------------------------------------------------- preflight


def test_require_cli_raises_a_config_error_when_the_binary_is_absent() -> None:
    with pytest.raises(ConfigError, match="not found"):
        require_cli("definitely-not-installed-cli-xyz", "codex")


def test_require_cli_returns_an_absolute_path_for_a_present_binary() -> None:
    assert Path(require_cli("git", "codex")).is_absolute()


def test_foundry_endpoint_reads_the_injected_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/p/demo/")
    assert foundry_endpoint() == "https://x.services.ai.azure.com/p/demo"


def test_foundry_endpoint_raises_when_the_platform_did_not_inject_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_AI_PROJECT_ENDPOINT", raising=False)
    with pytest.raises(ConfigError, match="FOUNDRY_PROJECT_ENDPOINT"):
        foundry_endpoint()


def test_real_harnesses_refuse_to_run_before_preflight(tmp_path: Path) -> None:
    for harness in (CodexHarness(), ClaudeCodeHarness()):
        with pytest.raises(RuntimeError, match="preflight"):
            harness.run(tmp_path, "p", 5)


# ------------------------------------------------------ codex / claude-code argv and env


STUB_TOKEN = "stub.foundry.inference.token"


@pytest.fixture
def stub_foundry_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent mints the model token now, so the harnesses need one to hand over."""
    for module in ("codex", "claude_code"):
        monkeypatch.setattr(
            f"ticket_to_pr.harness.{module}.foundry_token",
            lambda *, run_seconds: STUB_TOKEN,
        )


def _fake_run_cli(recorder: dict[str, object]) -> object:
    def fake(argv, *, repo_dir, prompt, timeout_s, env):  # type: ignore[no-untyped-def]
        recorder["argv"] = list(argv)
        recorder["env"] = dict(env)
        recorder["timeout_s"] = timeout_s
        recorder["prompt"] = prompt
        return subprocess.CompletedProcess(list(argv), 0, "the summary", "")

    return fake


@pytest.mark.usefixtures("stub_foundry_token")
def test_codex_argv_is_non_interactive_and_pins_the_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/p/demo")
    monkeypatch.setattr("ticket_to_pr.harness.codex.require_cli", lambda *_: "/usr/bin/codex")
    recorder: dict[str, object] = {}
    monkeypatch.setattr("ticket_to_pr.harness.codex.run_cli", _fake_run_cli(recorder))

    harness = CodexHarness()
    harness.preflight(settings)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = harness.run(repo, "the prompt", 120)

    argv = recorder["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in argv
    assert "workspace-write" in argv
    assert settings.model_deployment in argv
    assert 'approval_policy="never"' in argv
    # The bearer token is read out of the environment, not minted by codex: the harness
    # is never given the identity plumbing that would let it mint one
    # (docs/architecture.md §4.2).
    assert f'model_providers.foundry.env_key="{TOKEN_ENV}"' in argv
    assert not [flag for flag in argv if "auth.command" in flag]
    assert recorder["timeout_s"] == 120
    assert recorder["prompt"] == "the prompt"
    assert result.exit_code == 0


@pytest.mark.usefixtures("stub_foundry_token")
def test_codex_environment_carries_no_ado_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    for name, value in LEAKY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/p/demo")
    monkeypatch.setattr("ticket_to_pr.harness.codex.require_cli", lambda *_: "/usr/bin/codex")
    recorder: dict[str, object] = {}
    monkeypatch.setattr("ticket_to_pr.harness.codex.run_cli", _fake_run_cli(recorder))
    harness = CodexHarness()
    harness.preflight(settings)
    repo = tmp_path / "repo"
    repo.mkdir()
    harness.run(repo, "p", 10)
    env = recorder["env"]
    assert isinstance(env, dict)
    assert not [k for k in env if k.startswith(("T2PR_", "GIT_CONFIG_"))]
    assert "AZURE_DEVOPS_EXT_PAT" not in env
    assert "SYSTEM_ACCESSTOKEN" not in env
    # It carries one credential and it is an inference token, minted by the parent.
    assert env[TOKEN_ENV] == STUB_TOKEN
    assert "IDENTITY_ENDPOINT" not in env
    assert "MSI_SECRET" not in env
    assert "AZURE_FEDERATED_TOKEN_FILE" not in env


@pytest.mark.usefixtures("stub_foundry_token")
def test_claude_code_environment_sets_foundry_and_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/p/demo")
    monkeypatch.setattr(
        "ticket_to_pr.harness.claude_code.require_cli", lambda *_: "/usr/bin/claude"
    )
    recorder: dict[str, object] = {}
    monkeypatch.setattr("ticket_to_pr.harness.claude_code.run_cli", _fake_run_cli(recorder))
    harness = ClaudeCodeHarness()
    harness.preflight(settings)
    repo = tmp_path / "repo"
    repo.mkdir()
    harness.run(repo, "p", 10)

    env = recorder["env"]
    argv = recorder["argv"]
    assert isinstance(env, dict) and isinstance(argv, list)
    assert env["CLAUDE_CODE_USE_FOUNDRY"] == "1"
    assert env["ANTHROPIC_MODEL"] == settings.model_deployment
    # Model access is keyless -- no API keys (docs/configuration.md §4).
    assert "ANTHROPIC_FOUNDRY_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # docs/architecture.md §4.2: a pre-scoped inference token, not the identity plumbing that
    # would let the CLI resolve a credential -- and mint an Azure DevOps one -- itself.
    assert env["ANTHROPIC_FOUNDRY_AUTH_TOKEN"] == STUB_TOKEN
    assert "IDENTITY_ENDPOINT" not in env
    assert "AZURE_CLIENT_ID" not in env
    assert "--print" in argv
    assert "bypassPermissions" in argv


@pytest.mark.parametrize(
    ("stdout", "expected_summary_fragment", "failed"),
    [
        ('{"subtype":"success","is_error":false,"result":"Did the thing."}',
         "Did the thing.", False),
        ('{"subtype":"error_max_turns","is_error":true,"errors":["ran out"]}', "ran out", True),
        ('{"subtype":"success","is_error":true,"result":"partial"}', "partial", True),
        ("not json at all", "not json at all", False),
        ("", "", True),
    ],
)
def test_claude_code_output_parsing(
    stdout: str, expected_summary_fragment: str, failed: bool
) -> None:
    summary, is_failed = _parse_output(stdout)
    assert expected_summary_fragment in summary
    assert is_failed is failed


def test_harness_result_is_immutable() -> None:
    result = HarnessResult(exit_code=0, summary="s", stdout="o", stderr="e")
    with pytest.raises((AttributeError, TypeError)):
        result.exit_code = 1  # type: ignore[misc]
