"""Request profiles, User-Agent, robots.txt and anonymous access (DESIGN §9.1, §9.8)."""

from pathlib import Path

import pytest
import responses
from pyrate_limiter import Duration, Limiter, Rate

from pulseboard import __version__
from pulseboard.credentials import ANONYMOUS, Credentials
from pulseboard.errors import JiraUnavailableError, PermissionDeniedError, RateLimitedError
from pulseboard.settings import Limits, Settings
from pulseboard.sites import Site
from pulseboard.sources.jira.client import ANONYMOUS_ACCOUNT, JiraSource, _rate, user_agent
from pulseboard.sources.jira.models import JiraBoard

ASF = "https://issues.example.org/jira"
ROBOTS = "https://issues.example.org/robots.txt"
CLOUD = "https://acme.atlassian.net"
FAST = Limiter(Rate(10_000, Duration.SECOND))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def polite(site: Site, sleeps: Sleeps | None = None) -> JiraSource:
    limits = Settings().for_site(site).limits
    return JiraSource(
        site,
        ANONYMOUS,
        limits,
        limiter=FAST,
        sleep=sleeps or Sleeps(),
        cache_enabled=False,
        anonymous_time_zone="Europe/Berlin",
    )


def test_user_agent_names_the_version_and_contact(cloud_site: Site, asf_site: Site) -> None:
    assert user_agent(cloud_site) == f"pulseboard/{__version__}"
    assert user_agent(asf_site) == f"pulseboard/{__version__} (+{asf_site.contact})"


@responses.activate
def test_every_request_carries_the_user_agent(cloud_site: Site, cloud_creds: Credentials) -> None:
    responses.get(f"{CLOUD}/rest/agile/1.0/board/42", json={"id": 42, "name": "B", "type": "scrum"})

    JiraSource(cloud_site, cloud_creds, Limits(), limiter=FAST, cache_enabled=False).get(
        "rest/agile/1.0/board/42", JiraBoard
    )

    assert responses.calls[0].request.headers["User-Agent"] == f"pulseboard/{__version__}"


@responses.activate
def test_anonymous_site_sends_no_credentials(
    asf_site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine issues.example.org login leak password secret\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    responses.get(ROBOTS, status=404)
    responses.get(f"{ASF}/rest/agile/1.0/board/7", json={"id": 7, "name": "B", "type": "kanban"})

    polite(asf_site).get("rest/agile/1.0/board/7", JiraBoard)

    board_call = responses.calls[-1].request
    assert "Authorization" not in board_call.headers
    assert asf_site.contact is not None
    assert asf_site.contact in board_call.headers["User-Agent"]


def test_anonymous_identity_needs_no_call(asf_site: Site) -> None:
    user = polite(asf_site).current_user()  # the network guard fails any request

    assert user.identity == ANONYMOUS_ACCOUNT
    assert user.time_zone == "Europe/Berlin"


@responses.activate
def test_robots_txt_disallowed_path_is_never_called(asf_site: Site) -> None:
    responses.get(ROBOTS, body="User-agent: *\nDisallow: /jira/rest/agile/\n")
    board = responses.get(f"{ASF}/rest/agile/1.0/board/7", json={"id": 7})

    with pytest.raises(PermissionDeniedError, match="robots.txt") as info:
        polite(asf_site).get("rest/agile/1.0/board/7", JiraBoard)

    assert board.call_count == 0
    assert info.value.hint is not None
    assert "operator_permission" in info.value.hint


@responses.activate
def test_robots_txt_is_read_once_and_rules_for_other_agents_ignored(asf_site: Site) -> None:
    robots = responses.get(ROBOTS, body="User-agent: otherbot\nDisallow: /\n")
    responses.get(f"{ASF}/rest/agile/1.0/board/7", json={"id": 7, "name": "B", "type": "kanban"})
    src = polite(asf_site)

    for _ in range(2):
        src.get("rest/agile/1.0/board/7", JiraBoard)

    assert robots.call_count == 1
    assert src.disallowed_paths(["rest/api/2/issue"]) == ()


@responses.activate
def test_robots_txt_server_error_stops_a_polite_site(asf_site: Site) -> None:
    responses.get(ROBOTS, status=503)

    with pytest.raises(JiraUnavailableError, match="robots.txt"):
        polite(asf_site).get("rest/agile/1.0/board/7", JiraBoard)


@responses.activate
def test_operator_permission_skips_robots_txt(asf_site: Site) -> None:
    permitted = asf_site.model_copy(update={"operator_permission": "INFRA-1"})
    responses.get(f"{ASF}/rest/agile/1.0/board/7", json={"id": 7, "name": "B", "type": "kanban"})

    polite(permitted).get("rest/agile/1.0/board/7", JiraBoard)

    assert [str(c.request.url) for c in responses.calls] == [f"{ASF}/rest/agile/1.0/board/7"]


@responses.activate
def test_standard_sites_do_not_read_robots_txt(cloud_site: Site, cloud_creds: Credentials) -> None:
    src = JiraSource(cloud_site, cloud_creds, Limits(), limiter=FAST, cache_enabled=False)

    assert src.disallowed_paths(["rest/api/2/issue"]) == ()
    assert len(responses.calls) == 0


@responses.activate
def test_polite_site_stops_after_three_429s_with_longer_backoff(asf_site: Site) -> None:
    responses.get(ROBOTS, status=404)
    board = responses.get(f"{ASF}/rest/agile/1.0/board/7", status=429)
    sleeps = Sleeps()

    with pytest.raises(RateLimitedError):
        polite(asf_site, sleeps).get("rest/agile/1.0/board/7", JiraBoard)

    assert board.call_count == 3
    assert sleeps.calls == [5.0, 10.0]


@responses.activate
def test_a_success_resets_the_429_count(asf_site: Site) -> None:
    responses.get(ROBOTS, status=404)
    url = f"{ASF}/rest/agile/1.0/board/7"
    ok = {"id": 7, "name": "B", "type": "kanban"}
    for status in (429, 429, 200, 429, 429, 200):
        if status == 200:
            responses.get(url, json=ok)
        else:
            responses.get(url, status=status)
    src = polite(asf_site)

    src.get("rest/agile/1.0/board/7", JiraBoard)
    src.get("rest/agile/1.0/board/7", JiraBoard)


@pytest.mark.parametrize(("per_second", "limit", "interval"), [(4.0, 4, 1000), (0.5, 1, 2000)])
def test_rates(per_second: float, limit: int, interval: int) -> None:
    rate = _rate(per_second)

    assert (rate.limit, rate.interval) == (limit, interval)
