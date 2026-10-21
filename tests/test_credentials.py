"""Credential lookup: environment first, then keychain; never from settings (DESIGN §9.9.3)."""

import keyring.errors
import pytest
from pydantic import SecretStr

from fakes import FakeKeychain
from pulseboard.credentials import (
    ANONYMOUS,
    KEYCHAIN_SERVICE,
    CredentialSource,
    SystemKeychain,
    credential_source,
    env_var_name,
    keychain_username,
    resolve_credentials,
)
from pulseboard.domain.types import AuthMethod, Deployment, SiteId
from pulseboard.errors import AuthFailedError, ErrorCode
from pulseboard.sites import Site


def secret(value: SecretStr | None) -> str:
    assert value is not None
    return value.get_secret_value()


def test_cloud_credentials_from_environment(cloud_site: Site) -> None:
    environ = {"PULSEBOARD_ACME_EMAIL": "dev@example.com", "PULSEBOARD_ACME_TOKEN": "tok-123"}

    creds = resolve_credentials(cloud_site, environ, keychain=None)

    assert creds.email == "dev@example.com"
    assert secret(creds.token) == "tok-123"
    assert "tok-123" not in repr(creds)


def test_data_center_needs_no_email(dc_site: Site) -> None:
    creds = resolve_credentials(dc_site, {"PULSEBOARD_CORP_TOKEN": "pat"}, keychain=None)

    assert creds.email is None


def test_anonymous_site_needs_nothing(asf_site: Site) -> None:
    assert resolve_credentials(asf_site, {}, keychain=None) is ANONYMOUS
    assert credential_source(asf_site, {}, None) is CredentialSource.NOT_NEEDED


def test_keychain_fallback(cloud_site: Site) -> None:
    keychain = FakeKeychain(
        {
            (KEYCHAIN_SERVICE, "acme:email"): "dev@example.com",
            (KEYCHAIN_SERVICE, "acme:token"): "from-keychain",
        }
    )

    creds = resolve_credentials(cloud_site, {}, keychain)

    assert secret(creds.token) == "from-keychain"
    assert credential_source(cloud_site, {}, keychain) is CredentialSource.KEYCHAIN


def test_environment_wins_over_keychain(dc_site: Site) -> None:
    keychain = FakeKeychain({(KEYCHAIN_SERVICE, "corp:token"): "from-keychain"})
    environ = {"PULSEBOARD_CORP_TOKEN": "from-env"}

    creds = resolve_credentials(dc_site, environ, keychain)

    assert secret(creds.token) == "from-env"
    assert credential_source(dc_site, environ, keychain) is CredentialSource.ENVIRONMENT


def test_missing_credentials_name_where_to_set_them(cloud_site: Site) -> None:
    with pytest.raises(AuthFailedError) as info:
        resolve_credentials(cloud_site, {"PULSEBOARD_ACME_EMAIL": "a@b.c"}, FakeKeychain({}))

    assert info.value.code is ErrorCode.AUTH_FAILED
    assert info.value.hint is not None
    assert "PULSEBOARD_ACME_TOKEN" in info.value.hint
    assert "acme:token" in info.value.hint
    assert "pulseboard site add" in info.value.hint
    assert credential_source(cloud_site, {}, FakeKeychain({})) is CredentialSource.MISSING


def test_names_turn_hyphens_into_underscores() -> None:
    site = Site(
        site_id=SiteId("acme-cloud"),
        base_url="https://acme.atlassian.net",
        deployment=Deployment.CLOUD,
        auth_method=AuthMethod.API_TOKEN,
    )

    assert env_var_name(site, "token") == "PULSEBOARD_ACME_CLOUD_TOKEN"
    assert env_var_name(site, "email") == "PULSEBOARD_ACME_CLOUD_EMAIL"
    assert keychain_username(site, "token") == "acme-cloud:token"


def test_system_keychain_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_backend(*args: str) -> None:
        raise keyring.errors.NoKeyringError

    monkeypatch.setattr(keyring, "get_password", no_backend)
    monkeypatch.setattr(keyring, "delete_password", no_backend)
    chain = SystemKeychain()

    assert chain.get_password(KEYCHAIN_SERVICE, "acme:token") is None
    assert chain.delete_password(KEYCHAIN_SERVICE, "acme:token") is False
