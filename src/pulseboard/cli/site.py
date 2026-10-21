"""``pulseboard site add | list | test | remove`` (DESIGN §9.9.4).

Only ``site test`` and the optional check in ``site add`` make network calls, and only to the
configured site or the URL the user just entered. Secrets are read hidden, go to the keychain
(or stay in environment variables), and are never written to the settings file or printed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from pulseboard.cli.console import Console
from pulseboard.credentials import (
    ANONYMOUS,
    KEYCHAIN_SERVICE,
    Credentials,
    WritableKeychain,
    credential_fields,
    credential_source,
    env_var_name,
    keychain_username,
    resolve_credentials,
)
from pulseboard.domain.types import AuthMethod, Deployment, RequestProfile
from pulseboard.domain.untrusted import sanitize
from pulseboard.errors import ConfigError, ToolError
from pulseboard.origin import split_url
from pulseboard.settings import Settings
from pulseboard.settings_file import add_site, remove_site
from pulseboard.sites import Site, is_cloud_host
from pulseboard.sources.jira.client import JiraSource

Probe = Callable[[Site, Credentials, Settings], JiraSource]
"""Builds the Jira source used for read-only checks (replaced in tests)."""

READ_PATHS = (
    "rest/api/2/serverInfo",
    "rest/agile/1.0/board",
    "rest/api/2/issue",
    "rest/api/2/search",
    "rest/api/2/field",
    "rest/api/2/status",
)
"""The REST paths Pulseboard reads; a polite site's robots.txt must allow them."""
_LINK_MARKERS = frozenset(
    {"browse", "dashboards", "issues", "plugins", "projects", "rest", "secure", "servicedesk"}
)
"""Path segments that start Jira pages, so whatever precedes them is the context path."""
_DEPLOYMENT_TYPES = {Deployment.CLOUD: {"cloud"}, Deployment.DATA_CENTER: {"server", "datacenter"}}
_NAME_ECHO_MAX_CHARS = 60


def default_probe(site: Site, credentials: Credentials, settings: Settings) -> JiraSource:
    """A Jira source with the site's request profile and no HTTP cache."""
    return JiraSource(
        site,
        credentials,
        settings.for_site(site).limits,
        cache_enabled=False,
        anonymous_time_zone=settings.team.time_zone,
    )


def _error_line(error: ToolError) -> str:
    return f"{error.message} Hint: {error.hint}" if error.hint else error.message


# ---------------------------------------------------------------- site list


def list_sites(
    settings: Settings, console: Console, environ: Mapping[str, str], keychain: WritableKeychain
) -> int:
    """Print each configured site and where its credentials come from (never the value)."""
    if not settings.sites:
        console.say("No sites configured. Run `pulseboard site add`.")
        return 0
    for site in settings.sites:
        source = credential_source(site, environ, keychain)
        console.say(
            f"{site.site_id}  {site.origin}  {site.deployment}  auth={site.auth_method}  "
            f"profile={site.profile}  credentials={source}"
        )
    return 0


# ---------------------------------------------------------------- site test


@dataclass(frozen=True)
class Check:
    """One ``site test`` check."""

    name: str
    passed: bool
    detail: str


def _public(site: Site) -> Site:
    """The same site without credentials, for the public server-info endpoint."""
    return site.model_copy(update={"auth_method": AuthMethod.ANONYMOUS})


def check_site(
    site: Site,
    settings: Settings,
    environ: Mapping[str, str],
    keychain: WritableKeychain,
    probe: Probe,
) -> list[Check]:
    """Read-only checks: reachable over HTTPS, deployment type, sign-in, time zone, robots.txt."""
    checks: list[Check] = []
    try:
        info = probe(_public(site), ANONYMOUS, settings).server_info()
    except ToolError as exc:
        return [Check("reachable", passed=False, detail=_error_line(exc))]
    version = sanitize(info.version or "unknown version", _NAME_ECHO_MAX_CHARS)
    checks.append(Check("reachable", passed=True, detail=f"{site.origin} answers (Jira {version})"))
    reported = (info.deployment_type or "").lower()
    matches = reported in _DEPLOYMENT_TYPES[site.deployment]
    detail = f"site reports '{sanitize(reported or 'unknown', _NAME_ECHO_MAX_CHARS)}'"
    if not matches:
        detail += f"; the settings say '{site.deployment}'"
    checks.append(Check("deployment", passed=matches, detail=detail))

    time_zone = settings.team.time_zone
    if site.needs_credentials:
        try:
            user = probe(
                site, resolve_credentials(site, environ, keychain), settings
            ).current_user()
        except ToolError as exc:
            checks.append(Check("sign-in", passed=False, detail=_error_line(exc)))
            return checks
        name = sanitize(user.display_name or "the user", _NAME_ECHO_MAX_CHARS)
        checks.append(Check("sign-in", passed=True, detail=f"signed in as {name}"))
        time_zone = user.time_zone or time_zone
    checks.append(Check("time zone", passed=True, detail=time_zone))

    if site.profile is RequestProfile.POLITE:
        if site.operator_permission:
            detail = f"operator permission recorded ({site.operator_permission})"
            checks.append(Check("robots.txt", passed=True, detail=detail))
        else:
            try:
                blocked = probe(site, ANONYMOUS, settings).disallowed_paths(READ_PATHS)
            except ToolError as exc:
                checks.append(Check("robots.txt", passed=False, detail=_error_line(exc)))
            else:
                detail = f"disallows {', '.join(blocked)}" if blocked else "allows every read path"
                checks.append(Check("robots.txt", passed=not blocked, detail=detail))
    return checks


def run_test(
    site: Site,
    settings: Settings,
    console: Console,
    environ: Mapping[str, str],
    keychain: WritableKeychain,
    probe: Probe,
) -> int:
    """Print the checks for one site; exit status 1 if any failed."""
    checks = check_site(site, settings, environ, keychain, probe)
    for check in checks:
        console.say(f"{'PASS' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
    return 0 if all(c.passed for c in checks) else 1


# ---------------------------------------------------------------- site add


def derive_base_url(url: str) -> str:
    """The site base URL from a board or site link: the origin plus any context path.

    Raises:
        ValueError: if ``url`` is not an absolute HTTP(S) URL.
    """
    split = split_url(url)
    port = f":{split.port}" if split.port is not None else ""
    context: list[str] = []
    if not is_cloud_host(split.host):
        for segment in (s for s in split.path.split("/") if s):
            if segment in _LINK_MARKERS or segment.endswith((".jspa", ".jsp")):
                break
            context.append(segment)
    path = "/" + "/".join(context) if context else ""
    return f"{split.scheme}://{split.host}{port}{path}"


def suggest_site_id(base_url: str, taken: set[str]) -> str:
    """A site ID from the host's first label, unique among ``taken``."""
    label = split_url(base_url).host.split(".")[0].lower()
    cleaned = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in label).strip("-")
    base = (cleaned or "jira")[:28].ljust(2, "0")
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def _ask_site(settings: Settings, console: Console, probe: Probe) -> Site | None:
    try:
        base_url = derive_base_url(console.prompt("Board or site URL"))
    except ValueError as exc:
        console.warn(f"Not a usable link: {exc}")
        return None
    base_url = console.prompt("Base URL", base_url)
    try:
        host = split_url(base_url).host
    except ValueError as exc:
        console.warn(f"Not a usable base URL: {exc}")
        return None
    proposed = Deployment.CLOUD if is_cloud_host(host) else Deployment.DATA_CENTER
    answer = console.prompt("Deployment (cloud or datacenter)", proposed)
    try:
        deployment = Deployment(answer)
    except ValueError:
        console.warn("Deployment must be 'cloud' or 'datacenter'.")
        return None
    auth = AuthMethod.API_TOKEN
    if deployment is Deployment.DATA_CENTER:
        try:
            auth = AuthMethod(console.prompt("Auth method (pat or anonymous)", AuthMethod.PAT))
        except ValueError:
            console.warn("Auth method must be 'pat' or 'anonymous'.")
            return None
    entry: dict[str, str] = {
        "id": console.prompt(
            "Site ID", suggest_site_id(base_url, {s.site_id for s in settings.sites})
        ),
        "base_url": base_url,
        "deployment": deployment,
        "auth": auth,
    }
    if auth is AuthMethod.ANONYMOUS:
        entry["profile"] = RequestProfile.POLITE
        entry["contact"] = console.prompt("Contact URL or email for the User-Agent")
    try:
        site = Site.model_validate(entry)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            console.warn(f"  - {'.'.join(str(p) for p in error['loc']) or 'site'}: {error['msg']}")
        return None
    question = "Check the deployment type by reading the site's public server info?"
    if console.confirm(question, default=False):
        _verify_deployment(site, settings, console, probe)
    return site


def _verify_deployment(site: Site, settings: Settings, console: Console, probe: Probe) -> None:
    try:
        info = probe(_public(site), ANONYMOUS, settings).server_info()
    except ToolError as exc:
        console.warn(f"Could not read the server info: {_error_line(exc)}")
        return
    reported = (info.deployment_type or "unknown").lower()
    if reported in _DEPLOYMENT_TYPES[site.deployment]:
        console.say(f"The site reports '{reported}', as expected.")
    else:
        shown = sanitize(reported, _NAME_ECHO_MAX_CHARS)
        console.warn(f"The site reports '{shown}', not '{site.deployment}'. Check the deployment.")


def _store_secrets(site: Site, console: Console, keychain: WritableKeychain) -> None:
    fields = credential_fields(site)
    if not fields:
        return
    if not keychain.available():
        console.say("No OS keychain is available. Set these environment variables instead:")
        for field in fields:
            console.say(f"  {env_var_name(site, field)}")
        return
    for field in fields:
        value = console.prompt("Account email") if field == "email" else console.secret("Token")
        if value:
            keychain.set_password(KEYCHAIN_SERVICE, keychain_username(site, field), value)
    console.say(f"Stored the credentials in the OS keychain (service '{KEYCHAIN_SERVICE}').")


def add(  # noqa: PLR0913 - the command's collaborators, all injected for tests
    settings: Settings,
    path: Path,
    console: Console,
    environ: Mapping[str, str],
    keychain: WritableKeychain,
    probe: Probe,
) -> int:
    """Guided setup of one site: settings entry, credentials, optional test."""
    site = _ask_site(settings, console, probe)
    if site is None:
        return 2
    entry: dict[str, str | bool] = {
        "id": site.site_id,
        "base_url": site.base_url,
        "deployment": str(site.deployment),
        "auth": str(site.auth_method),
    }
    if site.profile is RequestProfile.POLITE:
        entry["profile"] = str(site.profile)
    if site.contact:
        entry["contact"] = site.contact
    try:
        updated = add_site(path, entry)
    except ConfigError as exc:
        console.warn(exc.message)
        return 2
    console.say(f"Added site '{site.site_id}' to {path}.")
    _store_secrets(site, console, keychain)
    if console.confirm(f"Run `pulseboard site test {site.site_id}` now?", default=True):
        return run_test(site, updated, console, environ, keychain, probe)
    return 0


# ---------------------------------------------------------------- site remove


def remove(
    settings: Settings,
    path: Path | None,
    site_id: str,
    console: Console,
    keychain: WritableKeychain,
) -> int:
    """Remove a site entry and, after confirmation, its keychain entries."""
    site = next((s for s in settings.sites if s.site_id == site_id), None)
    try:
        removed = path is not None and remove_site(path, site_id)
    except ConfigError as exc:
        console.warn(exc.message)
        return 2
    if not removed or site is None:
        console.warn(f"No site '{sanitize(site_id, _NAME_ECHO_MAX_CHARS)}' in the settings file.")
        return 2
    console.say(f"Removed site '{site_id}'. Its stored snapshots remain until purged.")
    fields = credential_fields(site)
    if fields and console.confirm("Also delete its keychain entries?", default=False):
        for field in fields:
            keychain.delete_password(KEYCHAIN_SERVICE, keychain_username(site, field))
        console.say("Deleted the keychain entries.")
    return 0
