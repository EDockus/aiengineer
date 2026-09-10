"""Environment-driven settings, validated at startup (docs/configuration.md §1).

Two knobs here are *not* in that table -- ``T2PR_ADO_BASE_URL`` and ``T2PR_DEV_MODE``.
They are documented separately in docs/configuration.md §3, and they exist so the whole
orchestration can be run against a local Azure DevOps stub with no Azure tenant
(docs/architecture.md §7). Because they weaken authentication,
they are built to fail closed: see :func:`_validate_dev_mode` for the full gauntlet a
process must pass before dev mode is allowed to switch on, and note that turning it on
requires an exact confirmation phrase, a loopback bind, a loopback ADO base URL, and
the *absence* of every marker that says "you are running in the cloud".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ADO_BASE_URL = "https://dev.azure.com"

#: The one value of ``T2PR_DEV_MODE`` that enables dev mode. Anything else non-empty is
#: a hard startup error rather than a silent "off", so a half-remembered ``=1`` is loud.
DEV_MODE_PHRASE = "i-understand-this-disables-auth"

#: Baked into the image by agent/Dockerfile. Its presence means "this filesystem is a
#: deployed container", and dev mode is refused unconditionally. Overriding it means
#: rebuilding or deliberately mounting over it -- never an accident.
PRODUCTION_MARKER = Path("/etc/ticket-to-pr/production")

#: Any of these in the environment means we are running on a cloud host, not a laptop.
#: ``FOUNDRY_PROJECT_ENDPOINT`` is injected by the Foundry platform (docs/architecture.md §2);
#: the rest are the standard Azure Container Apps / App Service / AKS / IMDS markers.
CLOUD_ENV_MARKERS = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_PROJECT_ENDPOINT",
    "CONTAINER_APP_NAME",
    "CONTAINER_APP_ENV_DNS_SUFFIX",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
    "KUBERNETES_SERVICE_HOST",
    "IDENTITY_ENDPOINT",
    "MSI_ENDPOINT",
    "AZURE_FEDERATED_TOKEN_FILE",
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})

#: Azure DevOps' fixed resource id. docs/deploy.md §3 -- the only scope we ever request.
ADO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing, malformed or unsafe."""


class HarnessName(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    #: Not one of the two shipped harnesses in docs/configuration.md section 1; it exists
    #: for the reason docs/architecture.md section 7 gives. Only selectable while dev mode
    #: is on; :func:`Settings.from_env` rejects it otherwise.
    FAKE = "fake"


class AuthMode(StrEnum):
    #: Production: an Azure DevOps Personal Access Token (docs/deploy.md §3). The Entra
    #: agent identity cannot be used here -- Azure DevOps will not entitle one.
    PAT = "pat"
    #: Dev only: a static, meaningless token handed to a loopback stub.
    DEV_STATIC = "dev-static"


@dataclass(frozen=True, slots=True)
class Settings:
    harness: HarnessName
    model_deployment: str
    allowed_orgs: frozenset[str]
    max_run_minutes: int
    max_files_changed: int
    pr_draft: bool
    pr_reviewers: tuple[str, ...]
    commit_author_name: str
    commit_author_email: str
    log_level: str
    host: str
    port: int
    ado_base_url: str
    auth_mode: AuthMode
    dev_mode: bool
    #: Azure DevOps Personal Access Token. A secret, and the one credential in this design
    #: (docs/deploy.md §3): Azure DevOps cannot entitle a Foundry Entra agent identity
    #: (VS403283) and that identity cannot federate into one it does accept (AADSTS700231).
    #: Empty only in dev mode, where the stub needs no real credential.
    ado_pat: str

    @property
    def max_run_seconds(self) -> int:
        return self.max_run_minutes * 60

    def org_allowed(self, organization: str) -> bool:
        """Allowlist check. An empty allowlist rejects everything (docs/configuration.md §1)."""
        return organization.casefold() in self.allowed_orgs

    def org_url(self, organization: str) -> str:
        return f"{self.ado_base_url}/{organization}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build and validate settings. Raises :class:`ConfigError` on anything wrong."""
        e = os.environ if env is None else env

        dev_mode = _dev_mode_requested(e)
        port = _int(e, "T2PR_PORT", 8000, minimum=1, maximum=65535)
        ado_base_url = _normalise_base_url(_str(e, "T2PR_ADO_BASE_URL", DEFAULT_ADO_BASE_URL))
        # Dev mode defaults to loopback so the developer does not have to remember; an
        # *explicit* non-loopback T2PR_HOST is still a hard refusal, not a silent override.
        default_host = "127.0.0.1" if dev_mode else "0.0.0.0"  # noqa: S104 - container default
        host = _str(e, "T2PR_HOST", default_host)

        if dev_mode:
            _validate_dev_mode(e, host=host, ado_base_url=ado_base_url)
            auth_mode = AuthMode.DEV_STATIC
        else:
            _validate_production(e, ado_base_url=ado_base_url)
            auth_mode = AuthMode.PAT

        harness = _harness(e, dev_mode=dev_mode)

        allowed_orgs = frozenset(o.casefold() for o in _csv(e, "T2PR_ALLOWED_ORGS"))
        if not allowed_orgs:
            raise ConfigError(
                "T2PR_ALLOWED_ORGS is required and must list at least one Azure DevOps "
                "organization. An empty allowlist rejects every request."
            )

        ado_pat = _str(e, "T2PR_ADO_PAT", "")

        model_deployment = _str(e, "T2PR_MODEL_DEPLOYMENT", "gpt-6")
        if not model_deployment:
            raise ConfigError("T2PR_MODEL_DEPLOYMENT must not be empty.")

        log_level = _str(e, "T2PR_LOG_LEVEL", "INFO").upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigError(
                f"T2PR_LOG_LEVEL={log_level!r} is not one of "
                "CRITICAL, ERROR, WARNING, INFO, DEBUG."
            )

        email = _str(e, "T2PR_COMMIT_AUTHOR_EMAIL", "ticket-to-pr@noreply.local")
        if "@" not in email or any(c.isspace() for c in email):
            raise ConfigError(f"T2PR_COMMIT_AUTHOR_EMAIL={email!r} is not an email address.")

        # Last, so a malformed port or interval reports its own error rather than this one.
        if not dev_mode and not ado_pat:
            raise ConfigError(
                "T2PR_ADO_PAT is required: Azure DevOps authentication uses a Personal "
                "Access Token (docs/deploy.md section 3 records why the Entra agent identity "
                "cannot be used). Create one with Work Items (Read), Code (Read & Write) "
                "and Pull Request (Read & Write) scoped to the organization in "
                "T2PR_ALLOWED_ORGS, then set T2PR_ADO_PAT."
            )

        return cls(
            harness=harness,
            model_deployment=model_deployment,
            allowed_orgs=allowed_orgs,
            ado_pat=ado_pat,
            max_run_minutes=_int(e, "T2PR_MAX_RUN_MINUTES", 20, minimum=1, maximum=240),
            max_files_changed=_int(e, "T2PR_MAX_FILES_CHANGED", 50, minimum=1, maximum=10_000),
            pr_draft=_bool(e, "T2PR_PR_DRAFT", default=True),
            pr_reviewers=tuple(_csv(e, "T2PR_PR_REVIEWERS")),
            commit_author_name=_str(e, "T2PR_COMMIT_AUTHOR_NAME", "Ticket-to-PR agent"),
            commit_author_email=email,
            log_level=log_level,
            host=host,
            port=port,
            ado_base_url=ado_base_url,
            auth_mode=auth_mode,
            dev_mode=dev_mode,
        )


# --------------------------------------------------------------------------------------
# dev mode gate
# --------------------------------------------------------------------------------------


def _dev_mode_requested(e: Mapping[str, str]) -> bool:
    """Interpret ``T2PR_DEV_MODE``. Unset means off; anything but the exact phrase is fatal."""
    raw = e.get("T2PR_DEV_MODE", "").strip()
    if not raw:
        return False
    if raw != DEV_MODE_PHRASE:
        raise ConfigError(
            "T2PR_DEV_MODE is set but is not the required confirmation phrase. "
            f"To run the local development stub set T2PR_DEV_MODE={DEV_MODE_PHRASE!r} "
            "exactly; unset it for any real deployment. Refusing to start rather than "
            "guessing which you meant."
        )
    return True


def _validate_dev_mode(e: Mapping[str, str], *, host: str, ado_base_url: str) -> None:
    """Every condition that must hold before authentication may be disabled.

    Each check is independently sufficient to refuse. The intent is that no single
    mistake -- a stray environment variable, a copied ``.env``, an image rebuilt from a
    dev branch -- can produce a running container that trusts unauthenticated callers.
    """
    if PRODUCTION_MARKER.exists():
        raise ConfigError(
            f"T2PR_DEV_MODE is set but {PRODUCTION_MARKER} exists, which marks this "
            "filesystem as a deployed container image. Dev mode is never available "
            "here. Refusing to start."
        )

    present = [name for name in CLOUD_ENV_MARKERS if e.get(name, "").strip()]
    if present:
        raise ConfigError(
            "T2PR_DEV_MODE is set but this process looks like it is running on a cloud "
            f"host (found {', '.join(present)}). Dev mode is for a developer laptop "
            "only. Refusing to start."
        )

    if _hostname(host) not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"T2PR_DEV_MODE requires a loopback bind address, got T2PR_HOST={host!r}. "
            "Dev mode disables authentication, so the socket must not be reachable off "
            "this machine. Refusing to start."
        )

    raw_base = e.get("T2PR_ADO_BASE_URL", "").strip()
    if not raw_base:
        raise ConfigError(
            "T2PR_DEV_MODE requires T2PR_ADO_BASE_URL to point at a local Azure DevOps "
            "stub (for example http://127.0.0.1:8080). Without it the agent would send "
            "a placeholder token to the real dev.azure.com. Refusing to start."
        )

    parts = urlsplit(ado_base_url)
    if parts.scheme not in {"http", "https"} or _hostname(parts.hostname or "") not in (
        LOOPBACK_HOSTS
    ):
        raise ConfigError(
            f"T2PR_DEV_MODE requires T2PR_ADO_BASE_URL to be a loopback URL, got "
            f"{ado_base_url!r}. Refusing to start."
        )


def _validate_production(e: Mapping[str, str], *, ado_base_url: str) -> None:
    """In the absence of dev mode, none of the dev knobs may be present at all."""
    if ado_base_url != DEFAULT_ADO_BASE_URL:
        raise ConfigError(
            f"T2PR_ADO_BASE_URL={ado_base_url!r} overrides the Azure DevOps endpoint, "
            "which is only permitted in dev mode. Unset it, or set "
            f"T2PR_DEV_MODE={DEV_MODE_PHRASE!r} on a local machine. Refusing to start."
        )
    for name in ("T2PR_DEV_ADO_TOKEN", "T2PR_DEV_ALLOW_UNAUTHENTICATED"):
        if e.get(name, "").strip():
            raise ConfigError(
                f"{name} is set but T2PR_DEV_MODE is not. Refusing to start rather than "
                "run with a half-configured development authentication path."
            )


def _harness(e: Mapping[str, str], *, dev_mode: bool) -> HarnessName:
    raw = _str(e, "T2PR_HARNESS", HarnessName.CODEX.value).strip().casefold()
    try:
        harness = HarnessName(raw)
    except ValueError:
        allowed = ", ".join(h.value for h in HarnessName if h is not HarnessName.FAKE)
        raise ConfigError(f"T2PR_HARNESS={raw!r} is not one of: {allowed}.") from None
    if harness is HarnessName.FAKE and not dev_mode:
        raise ConfigError(
            "T2PR_HARNESS=fake writes a canned change instead of calling a model and is "
            "only available in dev mode (docs/configuration.md section 3). Refusing to start."
        )
    return harness


# --------------------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------------------


def _str(e: Mapping[str, str], name: str, default: str) -> str:
    raw = e.get(name)
    return default if raw is None or not raw.strip() else raw.strip()


def _int(e: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = e.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer.") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name}={value} is outside the allowed range {minimum}..{maximum}.")
    return value


def _bool(e: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = e.get(name, "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (true/false).")


def _csv(e: Mapping[str, str], name: str) -> list[str]:
    return [part.strip() for part in e.get(name, "").split(",") if part.strip()]


def _normalise_base_url(value: str) -> str:
    """Strip a trailing slash so URL joining is predictable."""
    return value.strip().rstrip("/")


def _hostname(value: str) -> str:
    return value.strip().strip("[]").casefold()
