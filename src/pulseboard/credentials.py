"""Credential lookup by site ID: environment variables first, then the OS keychain (DESIGN §9.9.3).

Credentials are never read from the settings file and never stored by Pulseboard's database.
Anonymous sites need none, and nothing is ever sent to them.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

import keyring
from keyring.backends.fail import Keyring as FailKeyring
from keyring.errors import KeyringError
from pydantic import BaseModel, ConfigDict, SecretStr

from pulseboard.domain.types import AuthMethod
from pulseboard.errors import AuthFailedError
from pulseboard.sites import Site

KEYCHAIN_SERVICE = "pulseboard"


class Credentials(BaseModel):
    """Credentials for one Jira site. ``token`` never appears in reprs or logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: str | None
    """Cloud API tokens are used with the account email; Data Center PATs need none."""
    token: SecretStr | None
    """``None`` only for anonymous sites."""


ANONYMOUS = Credentials(email=None, token=None)


class CredentialSource(StrEnum):
    """Where a site's credentials were found (``pulseboard site list``); never the value."""

    ENVIRONMENT = "environment"
    KEYCHAIN = "keychain"
    NOT_NEEDED = "not needed"
    MISSING = "missing"


class Keychain(Protocol):
    """Read access to a credential store."""

    def get_password(self, service: str, username: str) -> str | None:
        """Return the stored secret, or ``None`` if absent."""
        ...


class WritableKeychain(Keychain, Protocol):
    """A credential store ``pulseboard site add`` and ``site remove`` can change."""

    def available(self) -> bool:
        """Whether secrets can be stored here (headless Linux often has no backend)."""
        ...

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store a secret."""
        ...

    def delete_password(self, service: str, username: str) -> bool:
        """Delete a secret; ``False`` if there was none."""
        ...


class SystemKeychain:
    """The OS keychain via ``keyring``.

    Headless Linux often has no keychain backend; lookups then return ``None`` and
    environment variables are the documented fallback.
    """

    def get_password(self, service: str, username: str) -> str | None:
        """Return the stored secret, or ``None`` if absent or no backend is available."""
        try:
            return keyring.get_password(service, username)
        except KeyringError:
            return None

    def available(self) -> bool:
        """Whether an OS keychain backend exists."""
        return not isinstance(keyring.get_keyring(), FailKeyring)

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store a secret in the OS keychain."""
        keyring.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> bool:
        """Delete a secret from the OS keychain; ``False`` if absent or no backend exists."""
        try:
            keyring.delete_password(service, username)
        except KeyringError:
            return False
        return True


def credential_fields(site: Site) -> tuple[str, ...]:
    """The credentials a site needs: email + token (Cloud), token (Data Center), or none."""
    match site.auth_method:
        case AuthMethod.API_TOKEN:
            return ("email", "token")
        case AuthMethod.PAT:
            return ("token",)
        case AuthMethod.ANONYMOUS | AuthMethod.OAUTH_3LO:
            return ()


def env_var_name(site: Site, field: str) -> str:
    """Environment variable for a site's credential: site ``acme-cloud`` →
    ``PULSEBOARD_ACME_CLOUD_TOKEN``."""
    return f"PULSEBOARD_{site.site_id.upper().replace('-', '_')}_{field.upper()}"


def keychain_username(site: Site, field: str) -> str:
    """Keychain entry for a site's credential, e.g. ``acme-cloud:token``."""
    return f"{site.site_id}:{field}"


def _find(
    site: Site, field: str, environ: Mapping[str, str], keychain: Keychain | None
) -> tuple[str, CredentialSource] | None:
    if value := environ.get(env_var_name(site, field)):
        return value, CredentialSource.ENVIRONMENT
    if keychain is not None and (
        value := keychain.get_password(KEYCHAIN_SERVICE, keychain_username(site, field))
    ):
        return value, CredentialSource.KEYCHAIN
    return None


def _lookup(site: Site, field: str, environ: Mapping[str, str], keychain: Keychain | None) -> str:
    found = _find(site, field, environ, keychain)
    if found is None:
        msg = f"No {field} configured for site '{site.site_id}'."
        hint = (
            f"Run `pulseboard site add`, or set {env_var_name(site, field)} (or the keychain "
            f"entry '{keychain_username(site, field)}' under service '{KEYCHAIN_SERVICE}')."
        )
        raise AuthFailedError(msg, hint=hint)
    return found[0]


def credential_source(
    site: Site, environ: Mapping[str, str], keychain: Keychain | None
) -> CredentialSource:
    """Where ``site``'s credentials would come from. When fields come from different places,
    the token's source is reported."""
    fields = credential_fields(site)
    if not fields:
        return CredentialSource.NOT_NEEDED
    sources = [_find(site, field, environ, keychain) for field in fields]
    if any(found is None for found in sources):
        return CredentialSource.MISSING
    return sources[-1][1] if sources[-1] else CredentialSource.MISSING


def resolve_credentials(
    site: Site, environ: Mapping[str, str], keychain: Keychain | None
) -> Credentials:
    """Find the credentials for ``site``; anonymous sites get none.

    Raises:
        AuthFailedError: if a required credential is missing, naming where to set it. The
            secret value is never echoed.
    """
    fields = credential_fields(site)
    if not fields:
        return ANONYMOUS
    email = _lookup(site, "email", environ, keychain) if "email" in fields else None
    token = _lookup(site, "token", environ, keychain)
    return Credentials(email=email, token=SecretStr(token))
