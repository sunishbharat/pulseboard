"""The store interface the services depend on: SQLite locally, memory in hosted Profile A.

``SnapshotStore`` (SQLAlchemy) and ``MemoryStore`` (``pulseboard.hosted.memory``) both satisfy it.
Retention and per-account purges are local-mode administration and stay on ``SnapshotStore``.
"""

from collections.abc import Sequence
from typing import Protocol

from pulseboard.domain.plan import ExportRecord, Plan
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId, ExportId, PlanId, SiteId
from pulseboard.store.repository import BoardState, StoredExport, StoredSnapshot


class Store(Protocol):
    """Snapshots, refresh state, plans and export records, keyed by (site, account)."""

    def save_snapshot(
        self, snapshot: BoardSnapshot, *, full_reload: bool, baseline_key: str | None = None
    ) -> int:
        """Store a snapshot and return its ID."""
        ...

    def get_snapshot(self, snapshot_id: int) -> StoredSnapshot | None:
        """A stored snapshot by ID."""
        ...

    def snapshot_by_hash(
        self, site: SiteId, account: AccountId, board: BoardId, snapshot_hash: str
    ) -> BoardSnapshot | None:
        """The newest stored snapshot of a board with this content hash."""
        ...

    def known_account(self, site: SiteId, board: BoardId) -> AccountId | None:
        """The account that last refreshed a board."""
        ...

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Remove a superseded incremental snapshot."""
        ...

    def plan_uses_snapshot(
        self, site: SiteId, account: AccountId, board: BoardId, snapshot_hash: str
    ) -> bool:
        """Whether a stored plan of the board was built from a snapshot with this hash."""
        ...

    def has_baseline(self, site: SiteId, account: AccountId, board: BoardId, key: str) -> bool:
        """Whether a baseline with ``key`` exists for the board."""
        ...

    def board_state(self, site: SiteId, account: AccountId, board: BoardId) -> BoardState | None:
        """Refresh bookkeeping for a board, if it was fetched before."""
        ...

    def set_board_state(
        self, site: SiteId, account: AccountId, board: BoardId, state: BoardState
    ) -> None:
        """Insert or replace a board's refresh bookkeeping."""
        ...

    def save_plan(self, plan: Plan) -> None:
        """Store an immutable plan."""
        ...

    def get_plan(self, plan_id: PlanId) -> Plan | None:
        """A plan by ID, or ``None`` if unknown or gone."""
        ...

    def latest_plan(
        self, site: SiteId, account: AccountId, boards: Sequence[BoardId], settings_hash: str
    ) -> Plan | None:
        """The newest plan for the same site, account, boards and settings."""
        ...

    def save_export(self, record: ExportRecord, path: str) -> None:
        """Store an export record."""
        ...

    def get_export(self, export_id: ExportId) -> StoredExport | None:
        """An export record by ID."""
        ...
