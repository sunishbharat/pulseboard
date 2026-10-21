"""Snapshot refresh strategy (DESIGN §9.5) and automatic baselines (ADR-009).

1. A snapshot younger than the refresh interval is served from the store: no Jira calls.
2. Otherwise fetch incrementally (issues updated since the last fetch minus an overlap; the source
   converts that instant into the Jira user's time zone) and merge by issue ID.
3. Every ``full_reload_every``-th refresh reloads the whole board, which drops issues that left
   the board's filter or were deleted.
4. The first snapshot of each active sprint (Scrum) or week (Kanban) is kept as a baseline.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import BoardId, BoardType, SiteId, SprintState
from pulseboard.engine.calendar import local_date
from pulseboard.settings import Limits
from pulseboard.sources.base import BoardSource, Progress
from pulseboard.store.protocol import Store
from pulseboard.store.repository import BoardState


@dataclass(frozen=True)
class Refreshed:
    """The outcome of a refresh."""

    snapshot: BoardSnapshot
    fetched: bool
    """False when the stored snapshot was fresh enough (no Jira calls)."""
    full_reload: bool = False
    baseline_key: str | None = None


def merge(previous: BoardSnapshot, delta: BoardSnapshot) -> BoardSnapshot:
    """Apply an incremental fetch to the previous snapshot.

    Board configuration, sprints, versions and epics come from the new fetch; issues, people,
    transitions and sprint changes are merged by identity (issue ID, account ID, entry ID).
    """
    tasks = {t.issue_id: t for t in previous.tasks} | {t.issue_id: t for t in delta.tasks}
    people = {p.account_id: p for p in previous.people} | {p.account_id: p for p in delta.people}
    transitions = {(t.issue_id, t.entry_id): t for t in (*previous.transitions, *delta.transitions)}
    sprint_changes = {
        (c.issue_id, c.entry_id, c.sprint_id): c
        for c in (*previous.sprint_changes, *delta.sprint_changes)
    }
    return BoardSnapshot(
        site_id=delta.site_id,
        board_id=delta.board_id,
        account_id=delta.account_id,
        board_name=delta.board_name,
        board_type=delta.board_type,
        columns=delta.columns,
        statuses=delta.statuses,
        estimation_mode=delta.estimation_mode,
        estimation_field=delta.estimation_field,
        people=tuple(people.values()),
        sprints=delta.sprints,
        versions=delta.versions,
        epics=delta.epics,
        tasks=tuple(tasks.values()),
        transitions=tuple(transitions.values()),
        sprint_changes=tuple(sprint_changes.values()),
        fetched_at=delta.fetched_at,
    )


def baseline_key(snapshot: BoardSnapshot, now: datetime, time_zone: str) -> str | None:
    """``sprint:<id>`` for a Scrum board's active sprint, ``week:<ISO week>`` for Kanban."""
    if snapshot.board_type is BoardType.SCRUM:
        active = [s.sprint_id for s in snapshot.sprints if s.state is SprintState.ACTIVE]
        return f"sprint:{min(active)}" if active else None
    year, week, _ = local_date(now, time_zone).isocalendar()
    return f"week:{year}-W{week:02d}"


class BoardRefresher:
    """Keeps the stored snapshot of each board current."""

    def __init__(
        self,
        store: Store,
        limits: Limits,
        time_zone: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._limits = limits
        self._time_zone = time_zone
        self._clock = clock

    def refresh(
        self, source: BoardSource, site: SiteId, board_id: BoardId, progress: Progress | None = None
    ) -> Refreshed:
        """The current snapshot of a board, fetching only when the stored one is too old."""
        account = source.account_id()
        now = self._clock()
        state = self._store.board_state(site, account, board_id)
        if state is not None and now - state.last_fetch_at < timedelta(
            seconds=self._limits.refresh_interval_s
        ):
            stored = self._store.get_snapshot(state.latest_snapshot_id)
            if stored is not None:
                return Refreshed(stored.snapshot, fetched=False)

        window_start = local_date(now, self._time_zone) - timedelta(
            days=self._limits.reporting_window_days
        )
        count = (state.refresh_count if state else 0) + 1
        previous = self._store.get_snapshot(state.latest_snapshot_id) if state else None
        full = previous is None or count % self._limits.full_reload_every == 0
        if full or state is None or previous is None:
            snapshot = source.fetch_snapshot(
                board_id,
                window_start=window_start,
                updated_since=None,
                fetched_at=now,
                progress=progress,
            )
            full = True
        else:
            since = state.last_fetch_at - timedelta(
                minutes=self._limits.incremental_overlap_minutes
            )
            delta = source.fetch_snapshot(
                board_id,
                window_start=window_start,
                updated_since=since,
                fetched_at=now,
                progress=progress,
            )
            snapshot = merge(previous.snapshot, delta)

        key = baseline_key(snapshot, now, self._time_zone)
        if key is not None and self._store.has_baseline(site, account, board_id, key):
            key = None
        snapshot_id = self._store.save_snapshot(snapshot, full_reload=full, baseline_key=key)
        if previous is not None and not previous.full_reload and previous.baseline_key is None:
            self._store.delete_snapshot(previous.snapshot_id)  # superseded incremental merge
        self._store.set_board_state(site, account, board_id, BoardState(now, count, snapshot_id))
        return Refreshed(snapshot, fetched=True, full_reload=full, baseline_key=key)
