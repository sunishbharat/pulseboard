"""Normalizer: Jira data → BoardSnapshot (DESIGN §4.2, §8)."""

import json
from datetime import UTC, date, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue, ValidationError

from pulseboard.credentials import Credentials
from pulseboard.domain.snapshot import BoardSnapshot, Person, Task
from pulseboard.domain.types import BoardType, EstimationMode, SiteId, SprintState, StatusCategory
from pulseboard.engine.calendar import calendar_from_settings, unknown_people
from pulseboard.services.refresh import merge
from pulseboard.settings import Limits, PersonSettings, TeamSettings
from pulseboard.sites import Site
from pulseboard.sources.jira.client import JiraSource
from pulseboard.sources.jira.models import JiraBoardData
from pulseboard.sources.jira.normalizer import coerce_points, normalize, settings_key

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
    assert with_subtasks.settings_key == settings_key(TeamSettings())
    assert without.settings_key == settings_key(TeamSettings(include_subtasks=False))


def test_settings_key_follows_the_settings_that_shape_snapshots() -> None:
    blocks = settings_key(TeamSettings())

    dependency = {"name": "Dependency", "blocker": "outward"}

    def key(*types: object) -> str:
        return settings_key(TeamSettings.model_validate({"dependency_link_types": list(types)}))

    assert settings_key(TeamSettings(time_zone="Europe/Berlin")) == blocks  # does not shape
    assert key("Blocks", dependency) != blocks
    assert key(dependency, "Blocks") == key("Blocks", dependency)
    assert key({**dependency, "blocker": "inward"}) != key(dependency)  # direction shapes
    # Jira names compare trimmed and case-insensitively, so these are the same settings.
    assert key("blocks") == key(" BLOCKS ") == blocks
    assert settings_key(TeamSettings(include_subtasks=False)) != blocks


# ---------------------------------------------------------------- board-wide fields (PB-01)

RANK = "customfield_10019"
LATER = datetime(2026, 10, 19, 7, tzinfo=UTC)


def raw_issue(
    n: int,
    rank: str,
    *,
    blocked_by: int | None = None,
    link: str = "Blocks",
    side: str = "inwardIssue",
    parent: int | None = None,
    assignee: str = "alice",
) -> dict[str, object]:
    links = (
        [{"type": {"name": link}, side: {"id": str(blocked_by), "key": f"ABC-{blocked_by}"}}]
        if blocked_by is not None
        else []
    )
    return {
        "id": str(n),
        "key": f"ABC-{n}",
        "fields": {
            "summary": f"t{n}",
            "status": {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
            "assignee": {"accountId": assignee},
            "issuetype": {"name": "Sub-task" if parent else "Story", "subtask": bool(parent)},
            "issuelinks": links,
            "created": "2026-10-01T09:00:00+00:00",
            RANK: rank,
            **({"parent": {"id": str(parent), "key": f"ABC-{parent}"}} if parent else {}),
        },
    }


def board_data(issues: list[dict[str, object]]) -> JiraBoardData:
    rank_field = {
        "id": RANK,
        "name": "Rank",
        "custom": True,
        "schema": {"custom": "com.pyxis.greenhopper.jira:gh-lexo-rank"},
    }
    return JiraBoardData.model_validate_json(
        json.dumps(
            {
                "myself": {"accountId": "acc-1"},
                "board": {"id": 42, "name": "B", "type": "scrum"},
                "configuration": {"columnConfig": {"columns": []}},
                "sprints": [],
                "versions": [],
                "epics": [],
                "epic_due_dates": [],
                "issues": issues,
                "histories": {},
                "statuses": [],
                "fields": [rank_field],
            }
        )
    )


def board_of(
    issues: list[dict[str, object]],
    team: TeamSettings | None = None,
    fetched_at: datetime = FETCHED,
) -> BoardSnapshot:
    return normalize(
        board_data(issues),
        site_id=SiteId("acme"),
        base_url="https://acme.atlassian.net",
        team=team or TeamSettings(),
        fetched_at=fetched_at,
    )


BOARD = [
    raw_issue(1, "0|a"),
    raw_issue(2, "0|b", blocked_by=1),
    raw_issue(3, "0|c", blocked_by=99),  # ABC-99 is not on the board
    raw_issue(4, "0|d", parent=1, assignee="bob"),  # a subtask: ABC-1 waits for it
    raw_issue(5, "0|e", blocked_by=4, assignee="carol"),
]


def test_incremental_merge_keeps_edges_to_unchanged_issues_and_ranks() -> None:
    """PB-01: only ABC-2 and ABC-5 changed; their blockers did not."""
    full = board_of(BOARD)
    delta = board_of([BOARD[1], BOARD[4]], fetched_at=LATER)
    merged = merge(full, delta)
    tasks: dict[int, Task] = {t.issue_id: t for t in merged.tasks}

    assert tasks[2].blocked_by == (1,)
    assert tasks[2].external_blockers == ()
    assert tasks[5].blocked_by == (4,)
    assert tasks[1].blocked_by == (4,)  # the subtask edge survives
    assert [b.issue_key for b in tasks[3].external_blockers] == ["ABC-99"]
    assert [t.board_rank for t in merged.tasks] == [0, 1, 2, 3, 4]
    assert merged.snapshot_hash == board_of(BOARD, fetched_at=LATER).snapshot_hash


@settings(max_examples=60, deadline=None)
@given(st.sets(st.sampled_from(range(len(BOARD)))))
def test_merging_any_delta_equals_a_full_refetch(updated: set[int]) -> None:
    """PB-01 property: merge(full, normalize(any updated subset)) == normalize(all)."""
    full = board_of(BOARD)
    delta = board_of([BOARD[i] for i in sorted(updated)], fetched_at=LATER)

    assert merge(full, delta).snapshot_hash == full.snapshot_hash


def test_a_blocker_that_joins_the_board_later_becomes_an_edge() -> None:
    """ABC-3's blocker ABC-99 arrives in a later delta: ABC-3 is then blocked on the board."""
    full = board_of(BOARD)
    merged = merge(full, board_of([raw_issue(99, "0|f")], fetched_at=LATER))
    tasks: dict[int, Task] = {t.issue_id: t for t in merged.tasks}

    assert tasks[3].blocked_by == (99,)
    assert tasks[3].external_blockers == ()


# ---------------------------------------------------------------- link types (PB-02, PB-06)


def team_with(*types: object) -> TeamSettings:
    return TeamSettings.model_validate({"dependency_link_types": list(types)})


@pytest.mark.parametrize(
    ("blocker", "side", "expected"),
    [
        # "ABC-2 depends on ABC-1" (outward text) → ABC-1 must finish first.
        ("outward", "outwardIssue", (1,)),
        # "ABC-2 is depended on by ABC-1" (inward text) → ABC-1 waits for ABC-2, not this edge.
        ("outward", "inwardIssue", ()),
        ("inward", "inwardIssue", (1,)),
        ("inward", "outwardIssue", ()),
    ],
)
def test_link_direction_follows_the_configured_blocker_end(
    blocker: str, side: str, expected: tuple[int, ...]
) -> None:
    """PB-02: a non-Blocks type schedules from the end it names, in both directions."""
    issues = [raw_issue(1, "0|a"), raw_issue(2, "0|b", blocked_by=1, link="Dependency", side=side)]
    snap = board_of(issues, team_with({"name": "Dependency", "blocker": blocker}))

    blocked: dict[int, tuple[int, ...]] = {t.issue_id: t.blocked_by for t in snap.tasks}
    assert blocked[2] == expected


def test_a_link_type_other_than_blocks_needs_a_direction() -> None:
    with pytest.raises(ValidationError, match="needs a direction"):
        team_with("Dependency")
    with pytest.raises(ValidationError, match="only once"):
        team_with("Blocks", " blocks ")
    with pytest.raises(ValidationError, match="blank"):
        team_with({"name": "  ", "blocker": "inward"})


@pytest.mark.parametrize("configured", ["Blocks", "blocks", "BLOCKS", " Blocks "])
def test_link_type_names_match_trimmed_and_case_insensitively(configured: str) -> None:
    """PB-06: the configured name differs from Jira's "Blocks" only by case or spaces."""
    snap = board_of([raw_issue(1, "0|a"), raw_issue(2, "0|b", blocked_by=1)], team_with(configured))

    blocked: dict[int, tuple[int, ...]] = {t.issue_id: t.blocked_by for t in snap.tasks}
    assert blocked[2] == (1,)
    assert snap.link_types == ("Blocks",)
    assert snap.settings_key == settings_key(TeamSettings())


# ---------------------------------------------------------------- Data Center people (PB-16)


def dc_board(team: TeamSettings) -> BoardSnapshot:
    issue = raw_issue(1, "0|a")
    fields = issue["fields"]
    assert isinstance(fields, dict)
    fields["assignee"] = {"key": "JIRAUSER10100", "name": "dana", "displayName": "Dana"}
    return board_of([issue], team)


@pytest.mark.parametrize("configured", ["JIRAUSER10100", "dana", "Dana"])
def test_data_center_people_settings_match_the_user_key_or_username(configured: str) -> None:
    team = TeamSettings.model_validate(
        {
            "people": [{"account_id": configured, "capacity": 0.5}],
            "leave": [{"account_id": configured, "start": "2026-10-20", "end": "2026-10-21"}],
        }
    )
    snap = dc_board(team)
    person = snap.people[0]
    calendar = calendar_from_settings(date(2026, 10, 19), team, 1, snap.people)

    assert person.account_id == "JIRAUSER10100"
    assert person.username == "dana"
    assert person.capacity == 0.5
    assert calendar.person(person.account_id).capacity == 0.5
    assert calendar.breaks(person.account_id)  # the leave applies
    assert unknown_people(team, snap.people) == ()


def test_people_settings_that_match_no_one_are_reported() -> None:
    team = TeamSettings.model_validate(
        {
            "people": [{"account_id": "nobody", "capacity": 0.5}],
            "leave": [{"account_id": "ghost", "start": "2026-10-20", "end": "2026-10-21"}],
        }
    )
    snap = dc_board(team)

    assert snap.people[0].capacity == 1.0
    assert unknown_people(team, snap.people) == ("ghost", "nobody")
