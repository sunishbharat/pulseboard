"""The calling user of one MCP request: credentials from headers, identity from Jira (§1A.3).

Credentials are taken from the request headers only; HTTP mode never falls back to environment
variables, the keychain or a shared account (ADR-023). The caller lives in a context variable for
the duration of one tool call or resource read, so worker threads started inside it see it too.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import SecretStr

from pulseboard.credentials import Credentials
from pulseboard.domain.types import AccountId, Deployment
from pulseboard.errors import AuthFailedError
from pulseboard.sites import Site

EMAIL_HEADER = "x-jira-user-email"
TOKEN_HEADER = "x-jira-api-token"  # noqa: S105 - a header name
PAT_HEADER = "x-jira-pat"
ACCESS_KEY_HEADER = "x-pulseboard-access-key"
MISSING_CREDENTIALS = "missing per-user Jira credentials"
_CLOUD_HINT = "Send X-Jira-User-Email and X-Jira-Api-Token with every request."
_DC_HINT = "Send X-Jira-Pat with every request."


def credentials_from_headers(headers: Mapping[str, str], site: Site) -> Credentials:
    """The caller's Jira credentials for ``site``.

    Raises:
        AuthFailedError: ``missing per-user Jira credentials`` if a required header is missing
            or empty (Cloud needs both email and token).
    """
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    if site.deployment is Deployment.CLOUD:
        email, token = lowered.get(EMAIL_HEADER), lowered.get(TOKEN_HEADER)
        if not email or not token:
            raise AuthFailedError(MISSING_CREDENTIALS, hint=_CLOUD_HINT)
        return Credentials(email=email, token=SecretStr(token))
    pat = lowered.get(PAT_HEADER)
    if not pat:
        raise AuthFailedError(MISSING_CREDENTIALS, hint=_DC_HINT)
    return Credentials(email=None, token=SecretStr(pat))


@dataclass(frozen=True)
class Caller:
    """The user behind the current request, as Jira identified them."""

    account_id: AccountId
    credentials: Credentials


_current: ContextVar[Caller | None] = ContextVar("pulseboard_caller", default=None)


@contextmanager
def caller_scope(caller: Caller) -> Iterator[None]:
    """Make ``caller`` the current caller for the enclosed work."""
    token = _current.set(caller)
    try:
        yield
    finally:
        _current.reset(token)


def current_caller() -> Caller:
    """The current caller.

    Raises:
        AuthFailedError: outside a request that carried credentials.
    """
    caller = _current.get()
    if caller is None:
        raise AuthFailedError(MISSING_CREDENTIALS)
    return caller


def current_account() -> AccountId:
    """The current caller's Jira account (the owner of every cache entry it may read)."""
    return current_caller().account_id
