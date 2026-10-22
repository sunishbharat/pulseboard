"""Memory-only, size-capped storage for Profile A (ADDENDUM-01 §1A.2, ADR-022).

Snapshots, plans and export records are keyed by (site, Jira account) like the SQLite store, and
expire after an hour or when the least recently used entries are evicted. Nothing is written to
disk. Durable features are off: no automatic baselines and no stale-plan fallback.
"""

import itertools
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cachetools import TTLCache

from pulseboard.domain.plan import ExportRecord, Plan
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId, ExportId, PlanId, SiteId
from pulseboard.errors import BoardTooLargeError
from pulseboard.store.repository import BoardState, StoredExport, StoredSnapshot

BoardKey = tuple[SiteId, AccountId, BoardId]


@dataclass(frozen=True)
class MemoryLimits:
    """Lifetimes and caps of the in-memory caches."""

    ttl_s: float = 3600.0
    """Plans, snapshots and exports live an hour (``plan_id`` validity, DESIGN §11.3)."""
    max_snapshots: int = 64
    max_plans: int = 256
    max_boards: int = 1024
    max_exports: int = 1024
    max_export_bytes: int = 256 * 1024 * 1024
    """All export content together."""


DEFAULT_LIMITS = MemoryLimits()


class MemoryStore:
    """The ``Store`` protocol in memory. Thread-safe: services call it from worker threads."""

    def __init__(
        self, limits: MemoryLimits = DEFAULT_LIMITS, timer: Callable[[], float] = time.monotonic
    ) -> None:
        ttl = limits.ttl_s
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._snapshots: TTLCache[int, StoredSnapshot] = TTLCache(limits.max_snapshots, ttl, timer)
        self._states: TTLCache[BoardKey, BoardState] = TTLCache(limits.max_boards, ttl, timer)
        self._plans: TTLCache[PlanId, Plan] = TTLCache(limits.max_plans, ttl, timer)
        self._exports: TTLCache[ExportId, StoredExport] = TTLCache(limits.max_exports, ttl, timer)

    # ------------------------------------------------------------ snapshots

    def save_snapshot(
        self, snapshot: BoardSnapshot, *, full_reload: bool, baseline_key: str | None = None
    ) -> int:
        """Keep a snapshot and return its ID."""
        with self._lock:
            snapshot_id = next(self._ids)
            self._snapshots[snapshot_id] = StoredSnapshot(
                snapshot_id, snapshot, baseline_key, full_reload
            )
            return snapshot_id

    def get_snapshot(self, snapshot_id: int) -> StoredSnapshot | None:
        """A snapshot by ID, if still cached."""
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def snapshot_by_hash(
        self, site: SiteId, account: AccountId, board: BoardId, snapshot_hash: str
    ) -> BoardSnapshot | None:
        """The newest cached snapshot of the caller's board with this content hash."""
        with self._lock:
            found = [
                stored
                for stored in self._snapshots.values()
                if (s := stored.snapshot).site_id == site
                and s.account_id == account
                and s.board_id == board
                and s.snapshot_hash == snapshot_hash
            ]
        return max(found, key=lambda stored: stored.snapshot_id).snapshot if found else None

    def known_account(self, site: SiteId, board: BoardId) -> AccountId | None:
        """Always ``None``: there is no stale-plan fallback without durable storage."""
        return None

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Drop a superseded incremental snapshot."""
        with self._lock:
            self._snapshots.pop(snapshot_id, None)

    def plan_uses_snapshot(
        self, site: SiteId, account: AccountId, board: BoardId, snapshot_hash: str
    ) -> bool:
        """Whether a cached plan of the caller's board was built from this snapshot."""
        with self._lock:
            return any(
                p.site_id == site
                and p.account_id == account
                and p.board_ids == (board,)
                and p.snapshot_hash == snapshot_hash
                for p in self._plans.values()
            )

    def has_baseline(self, site: SiteId, account: AccountId, board: BoardId, key: str) -> bool:
        """Always ``True``, so no automatic baseline is ever taken (they need durable storage)."""
        return True

    # ------------------------------------------------------------ refresh state

    def board_state(self, site: SiteId, account: AccountId, board: BoardId) -> BoardState | None:
        """Refresh bookkeeping for the caller's board."""
        with self._lock:
            return self._states.get((site, account, board))

    def set_board_state(
        self, site: SiteId, account: AccountId, board: BoardId, state: BoardState
    ) -> None:
        """Replace refresh bookkeeping for the caller's board."""
        with self._lock:
            self._states[(site, account, board)] = state

    # ------------------------------------------------------------ plans

    def save_plan(self, plan: Plan) -> None:
        """Keep a plan (an existing plan ID is kept as is)."""
        with self._lock:
            self._plans.setdefault(plan.plan_id, plan)

    def get_plan(self, plan_id: PlanId) -> Plan | None:
        """A plan by ID, if still cached. Owner checks are the service's job."""
        with self._lock:
            return self._plans.get(plan_id)

    def latest_plan(
        self, site: SiteId, account: AccountId, boards: Sequence[BoardId], settings_hash: str
    ) -> Plan | None:
        """Always ``None``: there is no stale-plan fallback without durable storage."""
        return None

    # ------------------------------------------------------------ exports

    def save_export(self, record: ExportRecord, path: str) -> None:
        """Keep an export record."""
        with self._lock:
            self._exports[record.export_id] = StoredExport(record, path)

    def get_export(self, export_id: ExportId) -> StoredExport | None:
        """An export record by ID, if still cached."""
        with self._lock:
            return self._exports.get(export_id)


class MemoryFiles:
    """Export content in memory, capped by total size (``ExportFiles`` protocol)."""

    def __init__(
        self, limits: MemoryLimits = DEFAULT_LIMITS, timer: Callable[[], float] = time.monotonic
    ) -> None:
        self._lock = threading.Lock()
        self._limit = limits.max_export_bytes
        self._content: TTLCache[str, bytes] = TTLCache(
            self._limit, limits.ttl_s, timer, getsizeof=len
        )

    def write(self, relative: str, content: bytes) -> None:
        """Keep an export's content, evicting the oldest exports if needed.

        Raises:
            BoardTooLargeError: the export alone is larger than the memory cap.
        """
        if len(content) > self._limit:
            msg = "The export is larger than this server's in-memory limit."
            raise BoardTooLargeError(msg, hint="Use a smaller report or run Pulseboard locally.")
        with self._lock:
            self._content[relative] = content

    def write_asset(self, relative: str, content: bytes) -> None:
        """Keep a shared asset unless it is already cached."""
        with self._lock:
            present = relative in self._content
        if not present:
            self.write(relative, content)

    def read(self, relative: str) -> bytes | None:
        """An export's content, if still cached."""
        with self._lock:
            return self._content.get(relative)
