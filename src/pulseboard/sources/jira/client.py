"""JiraSource: paged, read-only fetches from Jira Cloud or Data Center (DESIGN §9.2–§9.5).

- HTTP through ``atlassian-python-api`` on a ``requests-cache`` session that caches only board
  configuration, the field and status catalogues, sprints and versions.
- ``tenacity`` retries throttled (429) and failed (5xx, connection) calls, honoring Retry-After.
- ``pyrate-limiter`` throttles requests per site. Calls are sequential (≤ the per-site limit).
- Changelogs come a page of issues at a time from a JQL search with ``expand=changelog``; only an
  issue whose changelog Jira truncated (Cloud: 100 entries) is fetched again on its own.
- Redirects to any other origin are refused. Errors map to typed codes (DESIGN §12.5).
- Request profiles (DESIGN §9.8): every request carries the Pulseboard User-Agent (with the site's
  contact, if set); polite sites back off longer, stop after a few 429s in a row, and never call a
  path their ``robots.txt`` disallows (checked with ``urllib.robotparser``).
- Anonymous sites get no credentials and no ``.netrc`` lookup; their identity is ``anonymous``.
- Every response is parsed straight into typed models; raw dicts never leave this module.
"""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, TypeVar
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from atlassian import Jira
from pydantic import BaseModel, ValidationError
from pyrate_limiter import Duration, Limiter, Rate
from requests_cache import DO_NOT_CACHE, CachedSession
from requests_cache.policy import ExpirationPatterns
from tenacity import RetryCallState, Retrying, retry_if_exception_type

from pulseboard import __version__
from pulseboard.credentials import Credentials
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, AuthMethod, BoardId, Deployment, RequestProfile
from pulseboard.errors import (
    AuthFailedError,
    BoardNotFoundError,
    BoardTooLargeError,
    JiraUnavailableError,
    PermissionDeniedError,
    RateLimitedError,
    SiteNotConfiguredError,
)
from pulseboard.origin import Origin, split_url
from pulseboard.settings import Limits, Settings
from pulseboard.sites import Site
from pulseboard.sources.jira.models import (
    JiraBoard,
    JiraBoardConfig,
    JiraBoardData,
    JiraDueDateIssue,
    JiraEpic,
    JiraFieldList,
    JiraHistory,
    JiraIssue,
    JiraIssuePage,
    JiraIssueWithChangelog,
    JiraSearchPage,
    JiraServerInfo,
    JiraSprint,
    JiraStatusList,
    JiraUser,
    JiraValuesPage,
    JiraVersion,
)
from pulseboard.sources.jira.normalizer import normalize, settings_key

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")
Progress = Callable[[str, int, int | None], None]
"""Called with (stage, items done, total if known)."""

BASE_FIELDS = (
    "summary",
    "status",
    "assignee",
    "issuetype",
    "priority",
    "issuelinks",
    "duedate",
    "created",
    "updated",
    "resolutiondate",
    "parent",
    "sprint",
    "epic",
    "fixVersions",
    "timeoriginalestimate",
    "timeestimate",
)
RANK_FIELD_SCHEMA = "com.pyxis.greenhopper.jira:gh-lexo-rank"
_DONE_CATEGORY = "done"
_EPIC_SEARCH_CHUNK = 50
_SECONDS_PER_HOUR = 3600
_HTTP_CLIENT_ERROR = 400
_HTTP_SERVER_ERROR = 500
ANONYMOUS_ACCOUNT = AccountId("anonymous")
"""The identity of every anonymous user: they all see the same public data (DESIGN §9.2)."""


def user_agent(site: Site) -> str:
    """``pulseboard/<version>``, plus ``(+<contact>)`` when the site sets one (DESIGN §9.8)."""
    agent = f"pulseboard/{__version__}"
    return f"{agent} (+{site.contact})" if site.contact else agent


class ForeignRedirectError(Exception):
    """A response redirected to an origin other than the configured site."""


class _RetryableError(Exception):
    def __init__(self, status: int | None, retry_after_s: float | None) -> None:
        super().__init__(f"retryable Jira failure (status {status})")
        self.status = status
        self.retry_after_s = retry_after_s


class _ResponseLike(Protocol):
    """The parts of an HTTP response Pulseboard inspects."""

    @property
    def url(self) -> str: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def is_redirect(self) -> bool: ...


class SiteSession(CachedSession):
    """A cached session that refuses redirects leaving the site's origin."""

    def __init__(
        self,
        origin: Origin,
        *,
        cache_name: str,
        backend: str,
        urls_expire_after: ExpirationPatterns,
    ) -> None:
        super().__init__(
            cache_name=cache_name,
            backend=backend,
            expire_after=DO_NOT_CACHE,
            urls_expire_after=urls_expire_after,
        )
        self._origin = origin
        self.hooks["response"].append(self._refuse_foreign_redirect)

    def _refuse_foreign_redirect(
        self, response: _ResponseLike, *args: object, **kwargs: object
    ) -> None:
        """Response hook: runs before requests follows a redirect, so raising refuses it."""
        location = response.headers.get("Location")
        if not response.is_redirect or location is None:
            return
        resolved = urljoin(response.url, location)
        try:
            allowed = self._origin.matches(split_url(resolved))
        except ValueError:
            allowed = False
        if not allowed:
            raise ForeignRedirectError(resolved)


def cache_rules(ttl_configuration_s: int, ttl_sprints_s: int) -> ExpirationPatterns:
    """URL patterns cached by requests-cache; everything else is never cached."""
    return {
        "*/rest/agile/1.0/board/*/configuration": ttl_configuration_s,
        "*/rest/api/2/field": ttl_configuration_s,
        "*/rest/api/2/status": ttl_configuration_s,
        "*/rest/agile/1.0/board/*/sprint": ttl_sprints_s,
        "*/rest/agile/1.0/board/*/version": ttl_sprints_s,
    }


def jql_datetime(instant: datetime, time_zone: str | None) -> str:
    """A JQL date-time literal in the Jira user's profile time zone (minute precision)."""
    local = instant.astimezone(ZoneInfo(time_zone or "UTC"))
    return local.strftime("%Y/%m/%d %H:%M")


def _retry_after(response: _ResponseLike) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None  # an HTTP date; fall back to exponential backoff


class JiraSource:
    """Read-only access to one Jira site as one user."""

    def __init__(
        self,
        site: Site,
        credentials: Credentials,
        limits: Limits,
        *,
        cache_path: Path | None = None,
        cache_ttls: tuple[int, int] = (3600, 600),
        cache_enabled: bool = True,
        limiter: Limiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        anonymous_time_zone: str = "UTC",
    ) -> None:
        self._site = site
        self._limits = limits
        self._sleep = sleep
        self._anonymous_time_zone = anonymous_time_zone
        self._myself: JiraUser | None = None
        self._consecutive_429 = 0
        self._robots: RobotFileParser | None = None
        self._respect_robots = (
            site.profile is RequestProfile.POLITE and site.operator_permission is None
        )
        self._limiter = limiter or Limiter(_rate(limits.requests_per_second))
        self.session = SiteSession(
            site.origin,
            cache_name=str(cache_path) if cache_path else "pulseboard-http",
            backend="sqlite" if cache_path else "memory",
            urls_expire_after=cache_rules(*cache_ttls) if cache_enabled else {},
        )
        self.session.headers["User-Agent"] = user_agent(site)
        self._timeout = int(limits.jira_timeout_s)
        token = credentials.token.get_secret_value() if credentials.token else None
        if site.needs_credentials and not token:
            msg = f"No token configured for site '{site.site_id}'."
            raise AuthFailedError(msg, hint="Run `pulseboard site add`.")
        # tenacity is the only retry layer; the library's own Retry-After handling is off.
        match site.auth_method:
            case AuthMethod.API_TOKEN:
                self._jira = Jira(
                    url=site.base_url,
                    username=credentials.email,
                    password=token,
                    cloud=True,
                    session=self.session,
                    timeout=self._timeout,
                    retry_with_header=False,
                )
            case AuthMethod.PAT:
                self._jira = Jira(
                    url=site.base_url,
                    token=token,
                    cloud=False,
                    session=self.session,
                    timeout=self._timeout,
                    retry_with_header=False,
                )
            case AuthMethod.ANONYMOUS | AuthMethod.OAUTH_3LO:
                self._jira = Jira(
                    url=site.base_url,
                    cloud=site.deployment is Deployment.CLOUD,
                    session=self.session,
                    timeout=self._timeout,
                    retry_with_header=False,
                )
                # An auth handler that adds nothing, so requests never consults .netrc.
                self.session.auth = lambda request: request

    # ------------------------------------------------------------ transport

    def _wait(self, state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome else None
        if isinstance(error, _RetryableError) and error.retry_after_s is not None:
            return min(error.retry_after_s, self._limits.retry_max_wait_s)
        backoff = self._limits.retry_base_wait_s * 2.0 ** (state.attempt_number - 1)
        return min(backoff, self._limits.retry_max_wait_s)

    def _stop(self, state: RetryCallState) -> bool:
        cap = self._limits.max_consecutive_429
        if cap is not None and self._consecutive_429 >= cap:
            return True
        return state.attempt_number > self._limits.max_retries

    def _check_robots(self, path: str) -> None:
        """Stop before calling a path the site's robots.txt disallows (polite sites only)."""
        if self.disallowed_paths([path]):
            msg = f"robots.txt on site '{self._site.site_id}' disallows {path.split('?')[0]}."
            hint = "Ask the operator for permission and record it in operator_permission."
            raise PermissionDeniedError(msg, hint=hint)

    def disallowed_paths(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Which of ``paths`` the site's robots.txt disallows (empty unless it is respected)."""
        if not self._respect_robots:
            return ()
        if self._robots is None:
            self._robots = self._read_robots()
        base = self._site.base_url.rstrip("/") + "/"
        agent = user_agent(self._site)
        return tuple(p for p in paths if not self._robots.can_fetch(agent, urljoin(base, p)))

    def _read_robots(self) -> RobotFileParser:
        origin = self._site.origin
        port = f":{origin.port}" if origin.port is not None else ""
        url = f"{origin.scheme}://{origin.host}{port}/robots.txt"
        parser = RobotFileParser(url)
        self._limiter.try_acquire(self._site.site_id)
        try:
            response = self.session.get(url, timeout=self._timeout)
        except (ForeignRedirectError, OSError):
            response = None
        if response is not None and response.status_code < _HTTP_CLIENT_ERROR:
            parser.parse(response.text.splitlines())
        elif response is not None and response.status_code < _HTTP_SERVER_ERROR:
            parser.parse([])  # no robots.txt (4xx): everything is allowed (RFC 9309)
        else:
            msg = f"robots.txt on site '{self._site.site_id}' could not be read."
            raise JiraUnavailableError(msg, hint="Polite sites stop until it can; retry later.")
        return parser

    def _send(self, path: str, params: Mapping[str, object] | None) -> bytes:
        self._check_robots(path)
        self._limiter.try_acquire(self._site.site_id)
        try:
            response = self._jira.get(path, params=dict(params or {}), advanced_mode=True)
        except ForeignRedirectError:
            msg = "Jira redirected to a different site; the request was refused."
            raise SiteNotConfiguredError(msg, hint="Check the site's base_url.") from None
        except OSError as exc:  # requests' connection and timeout errors are OSErrors
            raise _RetryableError(None, None) from exc
        status = response.status_code
        self._consecutive_429 = self._consecutive_429 + 1 if status == 429 else 0
        if status < 300:
            return response.content
        if status == 401:
            msg = f"Jira rejected the credentials for site '{self._site.site_id}'."
            raise AuthFailedError(msg, hint="Ask the user to check their API token.")
        if status == 403:
            msg = "The Jira user cannot see this board or resource."
            raise PermissionDeniedError(msg)
        if status == 404:
            msg = "Jira found no such board."
            raise BoardNotFoundError(msg, hint="Check the board link.")
        if status == 429 or status >= 500:
            raise _RetryableError(status, _retry_after(response))
        msg = f"Jira returned HTTP {status}."
        raise JiraUnavailableError(msg, hint="Retry later.")

    def get(self, path: str, model: type[M], params: Mapping[str, object] | None = None) -> M:
        """GET ``path`` with retries and parse the response into ``model``."""
        retrying = Retrying(
            stop=self._stop,
            wait=self._wait,
            retry=retry_if_exception_type(_RetryableError),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            content = retrying(self._send, path, params)
        except _RetryableError as exc:
            if exc.status == 429:
                msg = "Jira is throttling requests."
                raise RateLimitedError(
                    msg, retry_after_s=exc.retry_after_s, hint="Retry later."
                ) from None
            msg = "Jira is unreachable or failing."
            raise JiraUnavailableError(msg, hint="Retry later.") from None
        try:
            return model.model_validate_json(content)
        except ValidationError:
            msg = f"Jira returned an unexpected response for {path.split('?')[0]}."
            raise JiraUnavailableError(msg) from None

    def _values(self, path: str, page_model: type[JiraValuesPage[T]]) -> tuple[T, ...]:
        found: list[T] = []
        while True:
            query = {"startAt": len(found), "maxResults": self._limits.jira_page_size}
            page = self.get(path, page_model, query)
            found.extend(page.values)
            if page.is_last or not page.values:
                return tuple(found)

    # ------------------------------------------------------------ endpoints

    def current_user(self) -> JiraUser:
        """The authenticated user (account identity and profile time zone); fetched once.

        Anonymous sites make no call: the identity is ``anonymous`` and the time zone is the
        team's (DESIGN §9.3).
        """
        if self._myself is None:
            if self._site.needs_credentials:
                self._myself = self.get("rest/api/2/myself", JiraUser)
            else:
                self._myself = JiraUser.model_validate(
                    {"key": ANONYMOUS_ACCOUNT, "timeZone": self._anonymous_time_zone}
                )
        return self._myself

    def server_info(self) -> JiraServerInfo:
        """The site's deployment type and version (``pulseboard site test``)."""
        return self.get("rest/api/2/serverInfo", JiraServerInfo)

    def _issues(
        self, board_id: int, jql: str, fields: Iterable[str], progress: Progress | None
    ) -> tuple[JiraIssue, ...]:
        found: list[JiraIssue] = []
        open_count = 0
        while True:
            query = {
                "jql": jql,
                "fields": ",".join(fields),
                "startAt": len(found),
                "maxResults": self._limits.jira_page_size,
            }
            page = self.get(f"rest/agile/1.0/board/{board_id}/issue", JiraIssuePage, query)
            found.extend(page.issues)
            open_count += sum(
                1 for i in page.issues if i.fields.status.status_category.key != _DONE_CATEGORY
            )
            if open_count > self._limits.max_open_issues:
                msg = f"The board has more than {self._limits.max_open_issues} open issues."
                raise BoardTooLargeError(msg, hint="Use a board with a narrower filter.")
            if progress:
                progress("issues", len(found), page.total)
            if not page.issues or len(found) >= page.total:
                return tuple(found)

    def _search(
        self,
        jql: str,
        query: Mapping[str, object],
        page_model: type[JiraSearchPage[T]],
        page_size: int,
    ) -> tuple[T, ...]:
        """Every issue a JQL search finds: token paging on Cloud, offset paging on DC."""
        found: list[T] = []
        if self._site.deployment is Deployment.CLOUD:
            token: str | None = None
            while True:
                params = {"jql": jql, **query, "maxResults": page_size}
                if token:
                    params["nextPageToken"] = token
                page = self.get("rest/api/3/search/jql", page_model, params)
                found.extend(page.issues)
                token = page.next_page_token
                if not token or not page.issues:
                    return tuple(found)
        while True:
            params = {"jql": jql, **query, "startAt": len(found), "maxResults": page_size}
            page = self.get("rest/api/2/search", page_model, params)
            found.extend(page.issues)
            if not page.issues or page.total is None or len(found) >= page.total:
                return tuple(found)

    def _histories(self, issue_id: str) -> tuple[JiraHistory, ...]:
        """One issue's full changelog, for an issue whose search result was truncated."""
        if self._site.deployment is Deployment.CLOUD:
            return self._values(
                f"rest/api/2/issue/{issue_id}/changelog", JiraValuesPage[JiraHistory]
            )
        issue = self.get(
            f"rest/api/2/issue/{issue_id}",
            JiraIssueWithChangelog,
            {"expand": "changelog", "fields": "status"},
        )
        return issue.changelog.histories

    def _changelogs(
        self, issue_ids: Sequence[str], progress: Progress | None
    ) -> dict[str, tuple[JiraHistory, ...]]:
        """Changelogs by issue ID, a page of issues per search with ``expand=changelog``;
        only truncated changelogs are fetched again per issue."""
        found: dict[str, tuple[JiraHistory, ...]] = {}
        size = self._limits.jira_page_size
        for start in range(0, len(issue_ids), size):
            chunk = issue_ids[start : start + size]
            jql = f"id in ({', '.join(chunk)})"
            query = {"fields": "status", "expand": "changelog"}
            for issue in self._search(jql, query, JiraSearchPage[JiraIssueWithChangelog], size):
                log = issue.changelog
                entries = self._histories(issue.id) if log.truncated else log.histories
                unique = {h.id: h for h in entries}
                found[issue.id] = tuple(sorted(unique.values(), key=lambda h: (h.created, h.id)))
            if progress:
                progress("changelogs", start + len(chunk), len(issue_ids))
        return found

    def _epic_due_dates(self, epics: Iterable[JiraEpic]) -> tuple[JiraDueDateIssue, ...]:
        keys = [e.key for e in epics if not e.done]
        found: list[JiraDueDateIssue] = []
        for start in range(0, len(keys), _EPIC_SEARCH_CHUNK):
            jql = f"issuekey in ({', '.join(keys[start : start + _EPIC_SEARCH_CHUNK])})"
            query = {"fields": "duedate"}
            found.extend(
                self._search(jql, query, JiraSearchPage[JiraDueDateIssue], _EPIC_SEARCH_CHUNK)
            )
        return tuple(found)

    def fetch_board(
        self,
        board_id: int,
        *,
        window_start: date,
        updated_since: datetime | None = None,
        progress: Progress | None = None,
    ) -> JiraBoardData:
        """Fetch a board's configuration, sprints, versions, epics and issues.

        Issues are those open, or resolved on or after ``window_start``; with ``updated_since``
        only issues updated since then (incremental refresh). Changelogs are fetched only for
        issues updated on or after ``window_start``.
        """
        myself = self.current_user()
        board = self.get(f"rest/agile/1.0/board/{board_id}", JiraBoard)
        configuration = self.get(f"rest/agile/1.0/board/{board_id}/configuration", JiraBoardConfig)
        sprints = (
            self._values(f"rest/agile/1.0/board/{board_id}/sprint", JiraValuesPage[JiraSprint])
            if board.type == "scrum"
            else ()
        )
        versions = self._values(
            f"rest/agile/1.0/board/{board_id}/version", JiraValuesPage[JiraVersion]
        )
        epics = self._values(f"rest/agile/1.0/board/{board_id}/epic", JiraValuesPage[JiraEpic])
        statuses = self.get("rest/api/2/status", JiraStatusList).root
        fields = self.get("rest/api/2/field", JiraFieldList).root

        extra_fields = [f.id for f in fields if f.schema_ and f.schema_.custom == RANK_FIELD_SCHEMA]
        if configuration.estimation and configuration.estimation.field:
            extra_fields.append(configuration.estimation.field.field_id)
        jql = f'(statusCategory != Done OR resolutiondate >= "{window_start:%Y-%m-%d}")'
        if updated_since is not None:
            jql += f' AND updated >= "{jql_datetime(updated_since, myself.time_zone)}"'
        issues = self._issues(board_id, jql, sorted({*BASE_FIELDS, *extra_fields}), progress)

        changed = [
            i.id
            for i in issues
            if i.fields.updated is None or i.fields.updated.date() >= window_start
        ]
        histories = self._changelogs(changed, progress)
        return JiraBoardData(
            myself=myself,
            board=board,
            configuration=configuration,
            sprints=sprints,
            versions=versions,
            epics=epics,
            epic_due_dates=self._epic_due_dates(epics),
            issues=issues,
            histories=histories,
            statuses=statuses,
            fields=fields,
        )


class JiraBoardSource:
    """``BoardSource`` over Jira: fetch with ``JiraSource``, then normalize."""

    def __init__(
        self,
        site: Site,
        credentials: Credentials,
        settings: Settings,
        *,
        cache_enabled: bool = True,
        persist_cache: bool = True,
    ) -> None:
        """``persist_cache=False`` (hosted mode) keeps the HTTP cache in this source's memory,
        so cached Jira responses are never shared between callers or written to disk."""
        self._site = site
        self._team = settings.team
        settings = settings.for_site(site)
        cache: Path | None = None
        if persist_cache:
            cache = settings.data_dir / f"http-cache-{site.site_id}.sqlite"
            cache.parent.mkdir(parents=True, exist_ok=True)
        self._jira = JiraSource(
            site,
            credentials,
            settings.limits,
            cache_path=cache,
            cache_ttls=(settings.http_cache.configuration_ttl_s, settings.http_cache.sprints_ttl_s),
            cache_enabled=cache_enabled,
            anonymous_time_zone=settings.team.time_zone,
        )
        self._account: AccountId | None = None

    def account_id(self) -> AccountId:
        """The authenticated user's identity; one ``/myself`` call per source."""
        if self._account is None:
            identity = self._jira.current_user().identity
            if not identity:
                msg = "Jira did not return an account identity for the current user."
                raise AuthFailedError(msg)
            self._account = AccountId(identity)
        return self._account

    def settings_key(self) -> str:
        """Key of the team settings the normalizer applies (link types, subtasks)."""
        return settings_key(self._team)

    def fetch_snapshot(
        self,
        board_id: BoardId,
        *,
        window_start: date,
        updated_since: datetime | None,
        fetched_at: datetime,
        progress: Progress | None = None,
    ) -> BoardSnapshot:
        """Fetch and normalize one board."""
        data = self._jira.fetch_board(
            board_id, window_start=window_start, updated_since=updated_since, progress=progress
        )
        return normalize(
            data,
            site_id=self._site.site_id,
            base_url=self._site.base_url,
            team=self._team,
            fetched_at=fetched_at,
        )


def _rate(requests_per_second: float) -> Rate:
    """A pyrate-limiter rate; below one request per second, one request per interval."""
    if requests_per_second >= 1:
        return Rate(int(requests_per_second), Duration.SECOND)
    return Rate(1, int(int(Duration.SECOND) / requests_per_second))


def jira_source_factory(
    site: Site, credentials: Credentials, settings: Settings
) -> JiraBoardSource:
    """Entry point for the ``pulseboard.sources`` group."""
    return JiraBoardSource(site, credentials, settings)
