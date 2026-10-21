"""Refresh strategy and automatic baselines (DESIGN §9.5, ADR-009; §18.1 #20)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from factories import snapshot, task
from fakes import Clock, FakeSource, T
from pulseboard.credentials import Credentials
from pulseboard.domain.snapshot import Sprint, StatusTransition
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    BoardType,
    IssueId,
    IssueKey,
    SiteId,
    SprintId,
    SprintState,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.services.refresh import BoardRefresher, baseline_key, merge
from pulseboard.settings import Limits, Settings
from pulseboard.sites import Site
from pulseboard.store.repository import SnapshotStore, create_store_engine, migrate

SITE, BOARD = SiteId("acme"), BoardId(42)


@pytest.fixture
def store() -> SnapshotStore:
    engine = create_store_engine("sqlite://")
    migrate(engine)
    return SnapshotStore(engine)


def refresher(store: SnapshotStore, clock: Clock, **limits: int) -> BoardRefresher:
    return BoardRefresher(store, Limits(**limits), "Europe/Berlin", clock)


def test_repeat_within_interval_makes_no_jira_calls(store: SnapshotStore) -> None:
    clock, source = Clock(), FakeSource([snapshot((task(1),))])
    r = refresher(store, clock)

    first = r.refresh(source, SITE, BOARD)
    clock.now += timedelta(seconds=119)
    second = r.refresh(source, SITE, BOARD)

    assert first.fetched
    assert first.full_reload
    assert not second.fetched
    assert second.snapshot == first.snapshot
    assert len(source.calls) == 1
    assert source.account_calls == 1


def test_incremental_refresh_window_and_moved_issue(store: SnapshotStore) -> None:
    """§18.1 #20: a moved issue (new key, same ID) keeps its identity across snapshots."""
    before = snapshot((task(7, issue_key=IssueKey("OLD-1")), task(8)))
    moved = snapshot((task(7, issue_key=IssueKey("NEW-9")),))
    clock, source = Clock(), FakeSource([before, moved])
    r = refresher(store, clock)

    r.refresh(source, SITE, BOARD)
    clock.now += timedelta(minutes=3)
    result = r.refresh(source, SITE, BOARD)

    assert result.fetched
    assert not result.full_reload
    assert source.calls[1].updated_since == T - timedelta(minutes=5)
    assert source.calls[1].window_start == date(2026, 7, 21)  # plan day in Berlin − 90 days
    keys = {t.issue_id: t.issue_key for t in result.snapshot.tasks}
    assert keys == {7: "NEW-9", 8: "ABC-8"}


def test_every_nth_refresh_is_a_full_reload(store: SnapshotStore) -> None:
    clock, source = Clock(), FakeSource([snapshot((task(1),))])
    r = refresher(store, clock, full_reload_every=3)
    kinds = []
    for _ in range(6):
        kinds.append(r.refresh(source, SITE, BOARD).full_reload)
        clock.now += timedelta(minutes=3)

    assert kinds == [True, False, True, False, False, True]


def test_superseded_incremental_snapshots_are_removed(store: SnapshotStore) -> None:
    clock, source = Clock(), FakeSource([snapshot((task(1),), board_type=BoardType.KANBAN)])
    r = refresher(store, clock)
    for _ in range(4):
        r.refresh(source, SITE, BOARD)
        clock.now += timedelta(minutes=3)

    state = store.board_state(SITE, AccountId("acc-1"), BOARD)
    assert state is not None
    assert state.refresh_count == 4
    # full reload (kept, also the weekly baseline) + the latest incremental merge only
    assert store.get_snapshot(1) is not None
    assert [store.get_snapshot(i) is not None for i in (2, 3, 4)] == [False, False, True]


def test_merge_combines_history_by_identity() -> None:
    def move(issue: int, entry: int) -> StatusTransition:
        return StatusTransition(
            issue_id=IssueId(issue), entry_id=entry, from_status_id="1", to_status_id="3", at=T
        )

    previous = snapshot((task(1), task(2)), transitions=(move(1, 10),))
    delta = snapshot((task(2, story_points=5.0),), transitions=(move(2, 20), move(1, 10)))

    merged = merge(previous, delta)

    assert [t.issue_id for t in merged.tasks] == [1, 2]
    assert merged.tasks[1].story_points == 5.0
    assert [(t.issue_id, t.entry_id) for t in merged.transitions] == [(1, 10), (2, 20)]


def sprint(state: SprintState) -> Sprint:
    return Sprint(sprint_id=SprintId(11), name=UntrustedText("S11"), state=state)


def test_baseline_keys() -> None:
    scrum = snapshot((task(1),), sprints=(sprint(SprintState.ACTIVE),))
    idle = snapshot((task(1),), sprints=(sprint(SprintState.FUTURE),))
    kanban = snapshot((task(1),), board_type=BoardType.KANBAN)

    assert baseline_key(scrum, T, "UTC") == "sprint:11"
    assert baseline_key(idle, T, "UTC") is None
    assert baseline_key(kanban, T, "UTC") == "week:2026-W43"


def test_automatic_baselines_are_taken_once(store: SnapshotStore) -> None:
    clock = Clock()
    source = FakeSource([snapshot((task(1),), sprints=(sprint(SprintState.ACTIVE),))])
    r = refresher(store, clock)

    first = r.refresh(source, SITE, BOARD)
    clock.now += timedelta(minutes=3)
    second = r.refresh(source, SITE, BOARD)

    assert first.baseline_key == "sprint:11"
    assert second.baseline_key is None
    assert len(store.baselines(SITE, AccountId("acc-1"), BOARD)) == 1


@pytest.mark.default_cassette("cloud_board.yaml")
@pytest.mark.vcr
def test_real_source_repeat_within_interval_makes_no_requests(
    store: SnapshotStore, cloud_site: Site, cloud_creds: Credentials, tmp_path: object
) -> None:
    """Replaying the Cloud cassette: any request on the second call would fail (replay only)."""
    from pathlib import Path

    from pulseboard.sources.jira.client import JiraBoardSource

    settings = Settings(data_dir=Path(str(tmp_path)), limits=Limits(jira_page_size=5))
    source = JiraBoardSource(cloud_site, cloud_creds, settings, cache_enabled=False)
    clock = Clock()
    clock.now = datetime(
        2026, 10, 19, 6, tzinfo=UTC
    )  # window start 2026-07-21 matches the cassette
    r = refresher(store, clock)

    first = r.refresh(source, SITE, BOARD)
    clock.now += timedelta(seconds=60)
    second = r.refresh(source, SITE, BOARD)

    assert len(first.snapshot.tasks) == 12
    assert not second.fetched
