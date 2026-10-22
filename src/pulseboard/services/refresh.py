"""Snapshot refresh strategy (DESIGN §9.5) and automatic baselines (ADR-009).

1. A snapshot younger than the refresh interval is served from the store: no Jira calls.
2. Otherwise fetch incrementally (issues updated since the last fetch minus an overlap; the source
   converts that instant into the Jira user's time zone) and merge by issue ID.
3. Every ``full_reload_every``-th refresh reloads the whole board, which drops issues that left
   the board's filter or were deleted. So does a change of the settings that shape snapshots
   (the source's ``settings_key``, for example the dependency link types) or a history window
   that starts earlier than the stored snapshot's: a stored snapshot fetched with other shaping
   is never served or merged into (ADDENDUM-03 §2).
4. The first snapshot of each active sprint (Scrum) or week (Kanban) is kept as a baseline.
5. A superseded incremental snapshot is deleted unless a stored plan still refers to it.

A merged snapshot equals a full refetch apart from ``fetched_at`` and ``window_start``: board-wide
fields are derived after the merge (``link_board``, ADDENDUM-03 §1).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pulseboard.domain.snapshot import BoardSnapshot, link_board
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
    transitions and sprint changes are merged by identity (issue ID, account ID, entry ID). Then
    the board-wide fields are derived again over the merged issues, and only people assigned to
    one of them are kept, as a full fetch would. The history window is the previous one: the
    delta only adds to it.
    """
    tasks = link_board(
        ({t.issue_id: t for t in previous.tasks} | {t.issue_id: t for t in delta.tasks}).values()
    )
    assignees = {t.assignee for t in tasks}
    people = {
        p.account_id: p for p in (*previous.people, *delta.people) if p.account_id in assignees
    }
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
        tasks=tasks,
        transitions=tuple(transitions.values()),
        sprint_changes=tuple(sprint_changes.values()),
        fetched_at=delta.fetched_at,
        settings_key=delta.settings_key,
        window_start=previous.window_start,
        link_types=(*previous.link_types, *delta.link_types),
    )


def _covers(snapshot: BoardSnapshot, shaping: str, window_start: date) -> bool:
    """Whether a stored snapshot was fetched with these shaping settings and this window."""
    return snapshot.settings_key == shaping and (
        snapshot.window_start is not None and snapshot.window_start <= window_start
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
        shaping = source.settings_key()
        now = self._clock()
        state = self._store.board_state(site, account, board_id)
        window_start = local_date(now, self._time_zone) - timedelta(
            days=self._limits.reporting_window_days
        )
        if state is not None and now - state.last_fetch_at < timedelta(
            seconds=self._limits.refresh_interval_s
        ):
            stored = self._store.get_snapshot(state.latest_snapshot_id)
            if stored is not None and _covers(stored.snapshot, shaping, window_start):
                return Refreshed(stored.snapshot, fetched=False)

        count = (state.refresh_count if state else 0) + 1
        previous = self._store.get_snapshot(state.latest_snapshot_id) if state else None
        full = (
            previous is None
            or count % self._limits.full_reload_every == 0
            or not _covers(previous.snapshot, shaping, window_start)
        )
        if full or state is None or previous is None:
            snapshot = source.fetch_snapshot(
                board_id,
                window_start=window_start,
                updated_since=None,
                fetched_at=now,
                progress=progress,
            ).model_copy(update={"window_start": window_start})
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
        if (
            previous is not None
            and not previous.full_reload
            and previous.baseline_key is None
            and not self._store.plan_uses_snapshot(
                site, account, board_id, previous.snapshot.snapshot_hash
            )
        ):
            self._store.delete_snapshot(previous.snapshot_id)  # superseded incremental merge
        self._store.set_board_state(site, account, board_id, BoardState(now, count, snapshot_id))
        return Refreshed(snapshot, fetched=True, full_reload=full, baseline_key=key)
