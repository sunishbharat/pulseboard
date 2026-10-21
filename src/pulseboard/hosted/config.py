"""Profile A settings from environment variables (ADDENDUM-01 §1A.5, ADR-024, ADR-025).

One Jira site per deployment, set with ``cf set-env``. Endpoint access keys come from the secret
store. Jira user credentials are never configuration in HTTP mode: they arrive per request.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError

from pulseboard.domain.types import AuthMethod, Deployment, SiteId
from pulseboard.errors import ConfigError
from pulseboard.sites import Site

TRANSPORT_ENV = "PULSEBOARD_TRANSPORT"
SITE_URL_ENV = "PULSEBOARD_SITE_URL"
SITE_DEPLOYMENT_ENV = "PULSEBOARD_SITE_DEPLOYMENT"
SITE_CLIENT_CERT_ENV = "PULSEBOARD_SITE_CLIENT_CERT"
ACCESS_KEYS_ENV = "PULSEBOARD_ACCESS_KEYS"
HOST_ENV = "PULSEBOARD_HOST"
PORT_ENV = "PULSEBOARD_PORT"
PUBLIC_URL_ENV = "PULSEBOARD_PUBLIC_URL"
MAX_SOLVES_ENV = "PULSEBOARD_MAX_CONCURRENT_SOLVES"

HOSTED_SITE_ID = SiteId("jira")
DEFAULT_HOST = "127.0.0.1"
"""Loopback unless the image sets ``PULSEBOARD_HOST=0.0.0.0`` (deploy/Dockerfile)."""
DEFAULT_PORT = 8765
MIN_ACCESS_KEY_CHARS = 16
DEFAULT_MAX_SOLVES = 1
MAX_SOLVES_LIMIT = 2
"""The solver, pandas and Plotly run in-process; more than two solves per instance would
outgrow the memory budget (ADDENDUM-01 §1A.6)."""
_MAX_PORT = 65535
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_CREDENTIAL_VARIABLE = re.compile(r"^PULSEBOARD_\w+_(TOKEN|PAT)$")


class Transport(StrEnum):
    """How the MCP server is reached."""

    STDIO = "stdio"
    HTTP = "http"


def transport_from(environ: Mapping[str, str]) -> Transport:
    """``PULSEBOARD_TRANSPORT``: ``stdio`` (default) or ``http``.

    Raises:
        ConfigError: for any other value.
    """
    value = environ.get(TRANSPORT_ENV, "").strip().lower() or Transport.STDIO
    try:
        return Transport(value)
    except ValueError:
        msg = f"{TRANSPORT_ENV} must be 'stdio' or 'http'."
        raise ConfigError(msg) from None


@dataclass(frozen=True)
class HostedConfig:
    """Everything Profile A reads from the environment."""

    site: Site
    access_keys: tuple[SecretStr, ...]
    host: str
    port: int
    public_url: str
    """Base of download and static links. Links are never built from request headers
    (``Host``, ``X-Forwarded-*``), which a caller controls."""
    max_concurrent_solves: int = DEFAULT_MAX_SOLVES


def _flag(environ: Mapping[str, str], name: str, problems: list[str]) -> bool:
    value = environ.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value not in _FALSE:
        problems.append(f"{name} must be true or false.")
    return False


def _site(environ: Mapping[str, str], problems: list[str]) -> Site | None:
    url = environ.get(SITE_URL_ENV, "").strip()
    deployment = environ.get(SITE_DEPLOYMENT_ENV, "").strip().lower()
    if not url:
        problems.append(f"{SITE_URL_ENV} is required (the Jira site's base URL).")
    if deployment not in {d.value for d in Deployment}:
        problems.append(f"{SITE_DEPLOYMENT_ENV} must be 'cloud' or 'datacenter'.")
    if problems:
        return None
    kind = Deployment(deployment)
    auth = AuthMethod.API_TOKEN if kind is Deployment.CLOUD else AuthMethod.PAT
    try:
        return Site(site_id=HOSTED_SITE_ID, base_url=url, deployment=kind, auth_method=auth)
    except ValidationError as exc:
        problems.extend(
            f"{SITE_URL_ENV}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        return None


def _access_keys(environ: Mapping[str, str], problems: list[str]) -> tuple[SecretStr, ...]:
    parts = environ.get(ACCESS_KEYS_ENV, "").split(",")
    keys = tuple(SecretStr(key) for part in parts if (key := part.strip()))
    if not keys:
        problems.append(f"{ACCESS_KEYS_ENV} is required (comma-separated endpoint access keys).")
    elif any(len(key.get_secret_value()) < MIN_ACCESS_KEY_CHARS for key in keys):
        problems.append(f"Each access key needs at least {MIN_ACCESS_KEY_CHARS} characters.")
    return keys


def _port(environ: Mapping[str, str], problems: list[str]) -> int:
    raw = environ.get(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) <= _MAX_PORT:
        problems.append(f"{PORT_ENV} must be a port number.")
        return DEFAULT_PORT
    return int(raw)


def _public_url(environ: Mapping[str, str], problems: list[str]) -> str:
    raw = environ.get(PUBLIC_URL_ENV, "").strip()
    if not raw:
        problems.append(
            f"{PUBLIC_URL_ENV} is required: the server's external base URL, used for download "
            "links (for example https://pulseboard.apps.internal)."
        )
        return ""
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        problems.append(
            f"{PUBLIC_URL_ENV} must be an http(s) URL with a host and no user, query or fragment."
        )
        return ""
    return raw.rstrip("/")


def _max_solves(environ: Mapping[str, str], problems: list[str]) -> int:
    raw = environ.get(MAX_SOLVES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_SOLVES
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_SOLVES_LIMIT:
        problems.append(f"{MAX_SOLVES_ENV} must be 1 or {MAX_SOLVES_LIMIT}.")
        return DEFAULT_MAX_SOLVES
    return int(raw)


def load_hosted_config(environ: Mapping[str, str]) -> HostedConfig:
    """Read and check the Profile A environment.

    Raises:
        ConfigError: listing every problem. Values are never echoed.
    """
    problems: list[str] = []
    site = _site(environ, problems)
    if _flag(environ, SITE_CLIENT_CERT_ENV, problems):
        problems.append(
            f"{SITE_CLIENT_CERT_ENV}=true: Jira client certificates are not supported yet."
        )
    keys = _access_keys(environ, problems)
    port = _port(environ, problems)
    public_url = _public_url(environ, problems)
    max_solves = _max_solves(environ, problems)
    if problems or site is None:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        msg = f"Invalid HTTP-mode settings:\n{listed}"
        raise ConfigError(msg)
    return HostedConfig(
        site=site,
        access_keys=keys,
        host=environ.get(HOST_ENV, "").strip() or DEFAULT_HOST,
        port=port,
        public_url=public_url,
        max_concurrent_solves=max_solves,
    )


def credential_variables(environ: Mapping[str, str]) -> list[str]:
    """Names of ``PULSEBOARD_*_TOKEN`` / ``*_PAT`` variables that are set; HTTP mode ignores
    them and warns at start (ADDENDUM-01 §1A.5, L28)."""
    return sorted(name for name in environ if _CREDENTIAL_VARIABLE.match(name))
