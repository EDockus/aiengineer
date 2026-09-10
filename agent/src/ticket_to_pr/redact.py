"""Scrub credential-shaped text out of anything that leaves this process.

Three things carry text the agent did not write and cannot vouch for: git's stderr, the
coding harness's ``summary``/``stdout``/``stderr``, and error messages built from either.
All three end up somewhere durable and human-facing -- a log line, a pull request
description in the customer's repository, an ``error.message`` on the wire -- so all three
go through :func:`redact` first.

The patterns are shape-based: the agent cannot enumerate every secret a harness might have
printed, but it can recognise the shapes that matter (an ``Authorization`` header in any
scheme, a URL with inline credentials, a JWT).

There is one secret the agent *does* know by value -- the Azure DevOps Personal Access
Token (docs/deploy.md §3) -- and shape alone will not catch it: a PAT is an
undistinguished alphanumeric string. :func:`register_secret` adds a literal to redact on
sight, and :mod:`ticket_to_pr.main` registers the PAT at startup. This matters more than it
did for a short-lived Entra token: a leaked PAT is valid until somebody revokes it.
"""

from __future__ import annotations

import re

#: ``Authorization: <scheme> <anything>`` in any casing. Not just ``bearer``: Azure DevOps
#: takes a PAT as HTTP ``Basic``, and git echoes the header it was given into stderr when a
#: fetch or push is refused.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*[a-z]+\s+)\S+")

#: ``https://user:password@host`` inline credentials.
_URL_CREDS_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")

#: A JWT. Every Entra token the agent handles is one, so this is the case that matters.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_.\-]{20,}")

PLACEHOLDER = "<redacted>"

#: Literal secrets to strike out wherever they appear. Populated at startup, never logged,
#: and deliberately module-level so every call site gets the protection without threading a
#: parameter through git_ops, prompt and run.
_KNOWN_SECRETS: set[str] = set()

#: Below this length a "secret" is more likely to be a substring of ordinary prose than a
#: credential, and blanking it would corrupt the text it appears in.
MIN_SECRET_LENGTH = 8


def register_secret(value: str) -> None:
    """Redact ``value`` on sight from now on. Safe to call more than once."""
    if value and len(value) >= MIN_SECRET_LENGTH:
        _KNOWN_SECRETS.add(value)


def redact(text: str) -> str:
    """Strip anything token-shaped, and any registered secret, out of untrusted text."""
    for secret in _KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, PLACEHOLDER)
    text = _AUTH_HEADER_RE.sub(rf"\1{PLACEHOLDER}", text)
    text = _URL_CREDS_RE.sub(rf"\1{PLACEHOLDER}@", text)
    return _JWT_RE.sub(PLACEHOLDER, text)


__all__ = ["MIN_SECRET_LENGTH", "PLACEHOLDER", "redact", "register_secret"]
