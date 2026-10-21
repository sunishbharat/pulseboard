"""Normalizer: Jira data → BoardSnapshot (DESIGN §4.2, §8)."""

from datetime import UTC, date, datetime

import pytest
from pydantic import JsonValue

from pulseboard.credentials import Credentials
from pulseboard.domain.snapshot import BoardSnapshot, Person, Task
from pulseboard.domain.types import BoardType, EstimationMode, SiteId, SprintState, StatusCategory
from pulseboard.settings import Limits, PersonSettings, TeamSettings
from pulseboard.sites import Site
from pulseboard.sources.jira.client import JiraSource
from pulseboard.sources.jira.models import JiraBoardData
from pulseboard.sources.jira.normalizer import coerce_points, normalize

FETCHED = datetime(2026, 10, 19, 6, tzinfo=UTC)


def fetch(site: Site, creds: Credentials, board_id: int) -> JiraBoardData:
    from pyrate_limiter import Duration, Limiter, Rate

    limiter = Limiter(Rate(10_000, Duration.SECOND))
    src = JiraSource(
        site,
        creds,
        Limits(jira_page_size=5),
        limiter=limiter,
        sleep=lambda _: None,
        cache_enabled=False,
    )
    return src.fetch_board(board_id, window_start=date(2026, 7, 21))


def snapshot_of(data: JiraBoardData, site: Site, team: TeamSettings | None = None) -> BoardSnapshot:
    return normalize(
        data,
        site_id=site.site_id,
        base_url=site.base_url,
        team=team or TeamSettings(),
        fetched_at=FETCHED,
    )


@pytest.mark.default_cassette("cloud_board.yaml")
@pytest.mark.vcr
def test_cloud_snapshot(cloud_site: Site, cloud_creds: Credentials) -> None:
    team = TeamSettings(people=(PersonSettings(account_id="557058:bob", capacity=0.5),))  # type: ignore[arg-type]  # AccountId is a str
    snap = snapshot_of(fetch(cloud_site, cloud_creds, 42), cloud_site, team)
    tasks: dict[int, Task] = {t.issue_id: t for t in snap.tasks}

    assert snap.board_type is BoardType.SCRUM
    assert snap.estimation_mode is EstimationMode.STORY_POINTS
    assert snap.account_id == "557058:alice"
    assert [c.name for c in snap.columns] == ["To Do", "In Progress", "In Review", "Done"]
    assert {s.status_id: s.category for s in snap.statuses}["10001"] is StatusCategory.IN_PROGRESS
    assert [s.state for s in snap.sprints] == [
        SprintState.CLOSED,
        SprintState.ACTIVE,
        SprintState.FUTURE,
    ]
    assert tasks[109].blocked_by == (106, 107)
    assert tasks[108].external_blockers[0].issue_key == "XYZ-7"
    assert tasks[110].story_points == 3.0  # string "3" coerced
    assert tasks[111].story_points is None
    assert tasks[111].invalid_estimate  # "two"
    assert tasks[111].assignee is None
    assert tasks[104].remaining_estimate_hours == 8.0
    assert tasks[104].epic_issue_id == 100
    assert tasks[104].fix_version_ids == (300,)
    assert tasks[101].url == "https://acme.atlassian.net/browse/ABC-101"
    assert [t.board_rank for t in snap.tasks] == list(range(12))
    assert snap.epics[0].due_date == date(2026, 11, 6)
    assert snap.versions[0].release_date == date(2026, 10, 30)
    people: dict[str, Person] = {p.account_id: p for p in snap.people}
    assert people["557058:bob"].capacity == 0.5
    assert len(snap.transitions) == 9  # 101, 102, 103, 105: two each; 104: one
    added = [c for c in snap.sprint_changes if c.issue_id == 108]
    assert added[0].added
    assert added[0].sprint_id == 11


@pytest.mark.default_cassette("dc_board.yaml")
@pytest.mark.vcr
def test_data_center_snapshot(dc_site: Site, dc_creds: Credentials) -> None:
    snap = snapshot_of(fetch(dc_site, dc_creds, 7), dc_site)
    tasks: dict[int, Task] = {t.issue_id: t for t in snap.tasks}

    assert snap.board_type is BoardType.KANBAN
    assert snap.estimation_mode is EstimationMode.TIME
    assert snap.account_id == "JIRAUSER10100"
    assert tasks[203].original_estimate_hours == 16.0
    assert tasks[203].blocked_by == (202,)
    assert tasks[202].epic_issue_id == 200
    assert snap.versions[0].release_date == date(2026, 11, 20)
    assert snap.epics[0].due_date == date(2026, 11, 13)
    assert tasks[201].url == "https://jira.corp.example:8443/jira/browse/OPS-201"
    assert len([t for t in snap.transitions if t.issue_id == 201]) == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, False)),
        (3, (3.0, False)),
        (2.5, (2.5, False)),
        (" 5 ", (5.0, False)),
        ("two", (None, True)),
        (-1, (None, True)),
        ("-2", (None, True)),
        (True, (None, True)),
        ([1], (None, True)),
    ],
)
def test_coerce_points(value: JsonValue, expected: tuple[float | None, bool]) -> None:
    assert coerce_points(value) == expected


def test_subtasks_are_dropped_or_roll_up(cloud_site: Site) -> None:
    def issue(n: str, subtask: bool, parent: str | None = None) -> dict[str, object]:
        return {
            "id": n,
            "key": f"ABC-{n}",
            "fields": {
                "status": {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
                "issuetype": {"name": "Sub-task" if subtask else "Story", "subtask": subtask},
                "created": "2026-10-01T09:00:00.000+0000",
                **({"parent": {"id": parent, "key": f"ABC-{parent}"}} if parent else {}),
            },
        }

    data = JiraBoardData.model_validate(
        {
            "myself": {"accountId": "a"},
            "board": {"id": 1, "name": "B", "type": "kanban"},
            "configuration": {"columnConfig": {"columns": []}},
            "sprints": [],
            "versions": [],
            "epics": [],
            "epic_due_dates": [],
            "issues": [issue("1", False), issue("2", True, "1")],
            "histories": {},
            "statuses": [],
            "fields": [],
        },
        strict=False,
    )
    with_subtasks = snapshot_of(data, cloud_site)
    without = snapshot_of(data, cloud_site, TeamSettings(include_subtasks=False))

    assert {t.issue_id: t.blocked_by for t in with_subtasks.tasks} == {1: (2,), 2: ()}
    assert [t.issue_id for t in without.tasks] == [1]
    assert with_subtasks.site_id == SiteId("acme")
