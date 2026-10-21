"""JiraSource: hand-built Cloud and Data Center cassettes, simulated errors (§9, §12.5)."""

from datetime import UTC, date, datetime

import pytest
import requests
import responses
from pyrate_limiter import Duration, Limiter, Rate

from pulseboard.credentials import Credentials
from pulseboard.errors import (
    AuthFailedError,
    BoardNotFoundError,
    BoardTooLargeError,
    JiraUnavailableError,
    PermissionDeniedError,
    RateLimitedError,
    SiteNotConfiguredError,
)
from pulseboard.settings import Limits
from pulseboard.sites import Site
from pulseboard.sources.jira.client import JiraSource, jql_datetime
from pulseboard.sources.jira.models import JiraBoard, JiraUser

WINDOW = date(2026, 7, 21)
CLOUD = "https://acme.atlassian.net"
FAST = Limiter(Rate(10_000, Duration.SECOND))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def source(
    site: Site,
    creds: Credentials,
    sleeps: Sleeps | None = None,
    *,
    cache: bool = True,
    **limits: int,
) -> JiraSource:
    return JiraSource(
        site,
        creds,
        Limits(jira_page_size=5, **limits),
        limiter=FAST,
        sleep=sleeps or Sleeps(),
        cache_enabled=cache,
    )


@pytest.mark.default_cassette("cloud_board.yaml")
@pytest.mark.vcr
def test_cloud_board(cloud_site: Site, cloud_creds: Credentials) -> None:
    progress: list[tuple[str, int, int | None]] = []
    data = source(cloud_site, cloud_creds, cache=False).fetch_board(
        42, window_start=WINDOW, progress=lambda *p: progress.append(p)
    )

    assert data.myself.identity == "557058:alice"
    assert data.board.type == "scrum"
    assert [s.id for s in data.sprints] == [10, 11, 12]
    assert len(data.issues) == 12  # three pages of five
    assert set(data.histories) == {i.id for i in data.issues}
    assert [h.id for h in data.histories["101"]] == ["1011", "1012", "1013"]
    assert data.epic_due_dates[0].fields.duedate == date(2026, 11, 6)
    assert ("issues", 12, 12) in progress
    assert progress[-1] == ("changelogs", 12, 12)


@pytest.mark.default_cassette("dc_board.yaml")
@pytest.mark.vcr
def test_data_center_board(dc_site: Site, dc_creds: Credentials) -> None:
    data = source(dc_site, dc_creds, cache=False).fetch_board(7, window_start=WINDOW)

    assert data.myself.identity == "JIRAUSER10100"
    assert data.sprints == ()  # kanban boards have no sprints
    assert [i.key for i in data.issues] == ["OPS-201", "OPS-202", "OPS-203"]
    assert [h.id for h in data.histories["201"]] == ["2011", "2012"]  # duplicate entry removed
    assert data.epic_due_dates[0].fields.duedate == date(2026, 11, 13)


def test_jql_datetime_uses_the_jira_user_time_zone() -> None:
    instant = datetime(2026, 10, 19, 8, 0, 59, tzinfo=UTC)

    assert jql_datetime(instant, "Europe/Berlin") == "2026/10/19 10:00"
    assert jql_datetime(instant, None) == "2026/10/19 08:00"


@responses.activate
def test_incremental_refresh_adds_updated_clause(
    cloud_site: Site, cloud_creds: Credentials
) -> None:
    src = source(cloud_site, cloud_creds)
    _register_board(scrum=False)
    responses.get(f"{CLOUD}/rest/agile/1.0/board/42/issue", json={"issues": [], "total": 0})

    src.fetch_board(42, window_start=WINDOW, updated_since=datetime(2026, 10, 19, 8, tzinfo=UTC))

    issue_call = next(c for c in responses.calls if "/issue" in str(c.request.url))
    assert "updated+%3E%3D+%222026%2F10%2F19+10%3A00%22" in str(issue_call.request.url)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthFailedError),
        (403, PermissionDeniedError),
        (404, BoardNotFoundError),
        (400, JiraUnavailableError),
    ],
)
@responses.activate
def test_http_errors_map_to_typed_codes(
    cloud_site: Site, cloud_creds: Credentials, status: int, error: type[Exception]
) -> None:
    responses.get(f"{CLOUD}/rest/agile/1.0/board/42", status=status, json={"errorMessages": ["x"]})

    with pytest.raises(error):
        source(cloud_site, cloud_creds).get("rest/agile/1.0/board/42", JiraBoard)


@responses.activate
def test_retry_after_is_honored(cloud_site: Site, cloud_creds: Credentials) -> None:
    url = f"{CLOUD}/rest/api/2/myself"
    responses.get(url, status=429, headers={"Retry-After": "7"})
    responses.get(url, status=503)
    responses.get(url, json={"accountId": "557058:alice"})
    sleeps = Sleeps()

    user = source(cloud_site, cloud_creds, sleeps).current_user()

    assert user.identity == "557058:alice"
    assert sleeps.calls == [7.0, 2.0]  # Retry-After, then exponential backoff


@responses.activate
def test_throttling_exhausts_into_rate_limited(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(f"{CLOUD}/rest/api/2/myself", status=429, headers={"Retry-After": "120"})
    sleeps = Sleeps()

    with pytest.raises(RateLimitedError) as info:
        source(cloud_site, cloud_creds, sleeps, max_retries=2).current_user()

    assert info.value.retry_after_s == 120.0
    assert sleeps.calls == [60.0, 60.0]  # capped at retry_max_wait_s
    assert len(responses.calls) == 3


@responses.activate
def test_server_errors_exhaust_into_unavailable(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(f"{CLOUD}/rest/api/2/myself", status=500)

    with pytest.raises(JiraUnavailableError):
        source(cloud_site, cloud_creds, max_retries=1).current_user()


@responses.activate
def test_connection_errors_exhaust_into_unavailable(
    cloud_site: Site, cloud_creds: Credentials
) -> None:
    responses.get(f"{CLOUD}/rest/api/2/myself", body=requests.ConnectionError("down"))

    with pytest.raises(JiraUnavailableError):
        source(cloud_site, cloud_creds, max_retries=1).current_user()


@responses.activate
def test_unexpected_response_shape(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(f"{CLOUD}/rest/agile/1.0/board/42", json={"id": "not-a-number"})

    with pytest.raises(JiraUnavailableError, match="unexpected response"):
        source(cloud_site, cloud_creds).get("rest/agile/1.0/board/42", JiraBoard)


@responses.activate
def test_redirect_to_another_origin_is_refused(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(
        f"{CLOUD}/rest/api/2/myself", status=302, headers={"Location": "https://evil.example/steal"}
    )
    evil = responses.get("https://evil.example/steal", json={"accountId": "x"})

    with pytest.raises(SiteNotConfiguredError, match="redirected"):
        source(cloud_site, cloud_creds).current_user()
    assert evil.call_count == 0


@responses.activate
def test_redirect_within_the_site_is_followed(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(
        f"{CLOUD}/rest/api/2/myself", status=302, headers={"Location": "/rest/api/2/myself/v2"}
    )
    responses.get(f"{CLOUD}/rest/api/2/myself/v2", json={"accountId": "557058:alice"})

    assert (
        source(cloud_site, cloud_creds).get("rest/api/2/myself", JiraUser).identity
        == "557058:alice"
    )


@responses.activate
def test_board_too_large(cloud_site: Site, cloud_creds: Credentials) -> None:
    _register_board(scrum=False)
    page = {"issues": [_open_issue(n) for n in range(5)], "total": 5, "startAt": 0, "maxResults": 5}
    responses.get(f"{CLOUD}/rest/agile/1.0/board/42/issue", json=page)

    with pytest.raises(BoardTooLargeError):
        source(cloud_site, cloud_creds, max_open_issues=3).fetch_board(42, window_start=WINDOW)


@responses.activate
def test_configuration_is_cached_but_issues_are_not(
    cloud_site: Site, cloud_creds: Credentials
) -> None:
    config = responses.get(
        f"{CLOUD}/rest/agile/1.0/board/42/configuration", json={"columnConfig": {"columns": []}}
    )
    issues = responses.get(
        f"{CLOUD}/rest/agile/1.0/board/42/issue", json={"issues": [], "total": 0}
    )
    src = source(cloud_site, cloud_creds)
    from pulseboard.sources.jira.models import JiraBoardConfig, JiraIssuePage

    for _ in range(2):
        src.get("rest/agile/1.0/board/42/configuration", JiraBoardConfig)
        src.get("rest/agile/1.0/board/42/issue", JiraIssuePage)

    assert config.call_count == 1
    assert issues.call_count == 2


def test_credentials_are_sent_as_configured(
    cloud_site: Site, dc_site: Site, cloud_creds: Credentials, dc_creds: Credentials
) -> None:
    assert source(cloud_site, cloud_creds).session.auth == ("alice@example.com", "cloud-token")
    assert source(dc_site, dc_creds).session.headers["Authorization"] == "Bearer dc-pat"


# ---------------------------------------------------------------- helpers


def _register_board(*, scrum: bool) -> None:
    board = f"{CLOUD}/rest/agile/1.0/board/42"
    responses.get(
        f"{CLOUD}/rest/api/2/myself",
        json={"accountId": "557058:alice", "timeZone": "Europe/Berlin"},
    )
    responses.get(board, json={"id": 42, "name": "B", "type": "scrum" if scrum else "kanban"})
    responses.get(f"{board}/configuration", json={"columnConfig": {"columns": []}})
    responses.get(f"{board}/version", json={"values": [], "isLast": True})
    responses.get(f"{board}/epic", json={"values": [], "isLast": True})
    responses.get(f"{CLOUD}/rest/api/2/status", json=[])
    responses.get(f"{CLOUD}/rest/api/2/field", json=[])


def _open_issue(n: int) -> dict[str, object]:
    return {
        "id": str(n),
        "key": f"ABC-{n}",
        "fields": {
            "status": {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
            "issuetype": {"name": "Story"},
            "created": "2026-10-01T09:00:00.000+0000",
        },
    }
