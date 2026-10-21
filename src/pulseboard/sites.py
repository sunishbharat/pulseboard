"""Configured Jira sites and the site registry with exact origin matching (DESIGN §9.1, §9.9.2).

A URL is accepted only if its normalized origin exactly matches a configured site. The
check is pure, so rejection always happens before any network call. With no sites
configured, every URL is rejected.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pulseboard.domain.types import AuthMethod, Deployment, RequestProfile, SiteId
from pulseboard.domain.untrusted import sanitize
from pulseboard.errors import InvalidBoardLinkError, SiteNotConfiguredError
from pulseboard.origin import Origin, SplitUrl, origin_from_base_url, split_url

CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "api_token",
        "api_key",
        "password",
        "pat",
        "secret",
        "email",
        "username",
        "credentials",
    }
)
CREDENTIAL_HINT = (
    "Use the OS keychain (run `pulseboard site add`) or PULSEBOARD_<SITE_ID>_EMAIL / "
    "PULSEBOARD_<SITE_ID>_TOKEN environment variables; see .env.example."
)
_CLOUD_DOMAINS = ("atlassian.net", "jira.com")
"""Hosts of Atlassian Cloud sites (``*.atlassian.net``, and older ``*.jira.com`` sites)."""
_CONTACT_MAX_CHARS = 200


def is_cloud_host(host: str) -> bool:
    """Whether ``host`` is an Atlassian Cloud site host."""
    return host.lower().endswith(tuple(f".{domain}" for domain in _CLOUD_DOMAINS))


class Site(BaseModel):
    """A configured Jira site. Board links are accepted only for these (DESIGN §9.1, §9.9.2).

    In the settings file the site ID is ``id`` and the auth method is ``auth``.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True
    )

    site_id: Annotated[
        SiteId,
        Field(alias="id", min_length=2, max_length=32, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$"),
    ]
    """Lowercase letters, digits and hyphens. Used in environment variable and keychain names."""
    base_url: str
    deployment: Deployment
    auth_method: Annotated[AuthMethod, Field(alias="auth")]
    profile: RequestProfile = RequestProfile.STANDARD
    """Defaults to ``polite`` for anonymous sites, where it is mandatory (DESIGN §9.8)."""
    contact: str | None = None
    """URL or email added to the User-Agent; required for the polite profile."""
    operator_permission: str | None = None
    """Reference to written permission from a public instance's operator (overrides robots.txt)."""
    allow_http: bool = False
    """Lab Data Center only: allow a plain HTTP base URL. Logged as a warning."""

    @model_validator(mode="before")
    @classmethod
    def _reject_credentials_and_default_profile(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        found = sorted(CREDENTIAL_KEYS.intersection(str(key).lower() for key in data))
        if found:
            msg = f"Credentials ({', '.join(found)}) must not be in the settings file. "
            raise ValueError(msg + CREDENTIAL_HINT)  # noqa: TRY004 - pydantic expects ValueError
        auth = data.get("auth", data.get("auth_method"))
        if "profile" not in data and auth == AuthMethod.ANONYMOUS:
            return {**data, "profile": RequestProfile.POLITE}
        return data

    @field_validator("contact")
    @classmethod
    def _check_contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        usable = value.isprintable() and not any(ch.isspace() for ch in value)
        if not usable or len(value) > _CONTACT_MAX_CHARS:
            msg = "contact must be a URL or email without spaces (it goes into the User-Agent)."
            raise ValueError(msg)
        if "@" not in value and not value.startswith(("https://", "http://")):
            msg = "contact must be an https:// URL or an email address."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_site(self) -> Self:
        origin = self.origin
        if origin.scheme == "http" and not self.allow_http:
            msg = "base_url must use https; set allow_http = true only for a lab Data Center."
            raise ValueError(msg)
        cloud_host = is_cloud_host(origin.host)
        if cloud_host and self.deployment is Deployment.DATA_CENTER:
            msg = f"{origin.host} is a Jira Cloud host; set deployment = 'cloud'."
            raise ValueError(msg)
        if not cloud_host and self.deployment is Deployment.CLOUD:
            msg = f"{origin.host} is not a Jira Cloud host; set deployment = 'datacenter'."
            raise ValueError(msg)
        allowed = {
            Deployment.CLOUD: (AuthMethod.API_TOKEN, AuthMethod.OAUTH_3LO),
            Deployment.DATA_CENTER: (AuthMethod.PAT, AuthMethod.ANONYMOUS),
        }[self.deployment]
        if self.auth_method not in allowed:
            names = " or ".join(f"'{a}'" for a in allowed)
            msg = f"A {self.deployment} site uses auth = {names}."
            raise ValueError(msg)
        if self.auth_method is AuthMethod.OAUTH_3LO:
            msg = "auth = 'oauth_3lo' needs hosted mode (v1.2); use 'api_token' locally."
            raise ValueError(msg)
        if self.allow_http and self.deployment is Deployment.CLOUD:
            msg = "allow_http is only for a lab Data Center."
            raise ValueError(msg)
        if self.auth_method is AuthMethod.ANONYMOUS and self.profile is not RequestProfile.POLITE:
            msg = "Anonymous sites must use profile = 'polite'."
            raise ValueError(msg)
        if self.profile is RequestProfile.POLITE and self.contact is None:
            msg = "A polite-profile site needs a contact (URL or email) for the User-Agent."
            raise ValueError(msg)
        return self

    @property
    def origin(self) -> Origin:
        """The normalized origin links are matched against."""
        return origin_from_base_url(self.base_url)

    @property
    def needs_credentials(self) -> bool:
        """Whether Pulseboard sends credentials to this site (never for anonymous sites)."""
        return self.auth_method is not AuthMethod.ANONYMOUS


_LIST_SITES_HINT = "Call pb_list_sites, or ask the user to add the site to the settings file."
_HOST_ECHO_MAX_CHARS = 60


class SiteRegistry:
    """The configured Jira sites."""

    def __init__(self, sites: Sequence[Site]) -> None:
        # Most specific context path first, so /jira wins over the bare host.
        self._sites = tuple(sorted(sites, key=lambda s: -len(s.origin.context_path)))
        self._by_id = {s.site_id: s for s in sites}

    @property
    def sites(self) -> tuple[Site, ...]:
        """Configured sites, ordered by site ID."""
        return tuple(sorted(self._sites, key=lambda s: s.site_id))

    def get(self, site_id: SiteId) -> Site:
        """The site with ``site_id``.

        Raises:
            SiteNotConfiguredError: if no such site is configured.
        """
        try:
            return self._by_id[site_id]
        except KeyError:
            msg = f"Site '{sanitize(site_id, _HOST_ECHO_MAX_CHARS)}' is not configured."
            raise SiteNotConfiguredError(msg, hint=_LIST_SITES_HINT) from None

    def match(self, url: str) -> tuple[Site, SplitUrl]:
        """The configured site that owns ``url``, and the split URL.

        Raises:
            InvalidBoardLinkError: if ``url`` is not a usable HTTP(S) URL.
            SiteNotConfiguredError: if no configured site has exactly this origin.
        """
        try:
            split = split_url(url)
        except ValueError as exc:
            raise InvalidBoardLinkError(
                str(exc), hint="Ask the user for the board's link from the browser."
            ) from None
        for site in self._sites:
            if site.origin.matches(split):
                return site, split
        host = sanitize(split.host, _HOST_ECHO_MAX_CHARS)
        if any(s.origin.same_server(split) and s.origin.owns_path(split.path) for s in self._sites):
            msg = f"Links to '{host}' must use {self._scheme_for(split)}."
            raise SiteNotConfiguredError(msg, hint="Use the site's https:// link.")
        msg = f"'{host}' is not a configured Jira site."
        raise SiteNotConfiguredError(msg, hint=_LIST_SITES_HINT)

    def _scheme_for(self, split: SplitUrl) -> str:
        schemes = {s.origin.scheme for s in self._sites if s.origin.same_server(split)}
        return " or ".join(f"{scheme}://" for scheme in sorted(schemes))
