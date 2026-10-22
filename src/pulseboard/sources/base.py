"""The ``BoardSource`` protocol (DESIGN §4.4, entry-point group ``pulseboard.sources``)."""

from collections.abc import Callable
from datetime import date, datetime
from importlib.metadata import entry_points
from typing import Protocol

from pulseboard.credentials import Credentials
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId
from pulseboard.settings import Settings
from pulseboard.sites import Site

ENTRY_POINT_GROUP = "pulseboard.sources"
Progress = Callable[[str, int, int | None], None]


class BoardSource(Protocol):
    """Read-only access to boards on one site, as one user."""

    def account_id(self) -> AccountId:
        """The authenticated user's account identity (fetched once per source)."""
        ...

    def fetch_snapshot(
        self,
        board_id: BoardId,
        *,
        window_start: date,
        updated_since: datetime | None,
        fetched_at: datetime,
        progress: Progress | None = None,
    ) -> BoardSnapshot:
        """A snapshot of the board; with ``updated_since``, only issues updated since then."""
        ...

    def settings_key(self) -> str:
        """Identifies the settings that shape this source's snapshots. A stored snapshot with
        another key is stale as a whole, so the board is fetched in full again."""
        ...


SourceFactory = Callable[[Site, Credentials, Settings], BoardSource]
"""Builds a source for a site (registered per deployment type)."""


def load_source_factories() -> dict[str, SourceFactory]:
    """Source factories registered in the ``pulseboard.sources`` entry-point group."""
    return {entry.name: entry.load() for entry in entry_points(group=ENTRY_POINT_GROUP)}
