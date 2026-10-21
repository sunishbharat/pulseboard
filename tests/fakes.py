"""Test doubles for sources and clocks."""

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId
from pulseboard.errors import JiraUnavailableError
from pulseboard.sources.base import Progress

T = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
ENV = {"PULSEBOARD_ACME_EMAIL": "alice@example.com", "PULSEBOARD_ACME_TOKEN": "t"}
LINK = "https://acme.atlassian.net/jira/software/projects/ABC/boards/42"


@dataclass
class Call:
    updated_since: datetime | None
    window_start: date


@dataclass
class FakeSource:
    """Serves prepared snapshots and records every call."""

    snapshots: list[BoardSnapshot]
    account_calls: int = 0
    calls: list[Call] = field(default_factory=list)
    unavailable: bool = False
    delay_s: float = 0.0
    _account: AccountId | None = None

    def account_id(self) -> AccountId:
        if self._account is None:
            self.account_calls += 1
            self._account = AccountId("acc-1")
        return self._account

    def fetch_snapshot(
        self,
        board_id: BoardId,
        *,
        window_start: date,
        updated_since: datetime | None,
        fetched_at: datetime,
        progress: Progress | None = None,
    ) -> BoardSnapshot:
        if self.unavailable:
            msg = "Jira is unreachable or failing."
            raise JiraUnavailableError(msg)
        if self.delay_s:
            time.sleep(self.delay_s)
        if progress is not None:
            progress("issues", 1, 1)
        self.calls.append(Call(updated_since, window_start))
        snap = self.snapshots[min(len(self.calls) - 1, len(self.snapshots) - 1)]
        return snap.model_copy(update={"fetched_at": fetched_at})


class FakeKeychain:
    """An in-memory keychain; ``available=False`` behaves like headless Linux."""

    def __init__(
        self, entries: dict[tuple[str, str], str] | None = None, *, available: bool = True
    ) -> None:
        self.entries = dict(entries or {})
        self._available = available

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def available(self) -> bool:
        return self._available

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> bool:
        return self.entries.pop((service, username), None) is not None


class Clock:
    def __init__(self) -> None:
        self.now = T

    def __call__(self) -> datetime:
        return self.now
