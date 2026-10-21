"""Board snapshot models: normalized Jira data for one board (DESIGN §8).

Identity rules: issues by ``IssueId`` (never key), people by ``AccountId`` (never name or
email), sites by ``SiteId``. Collections are sorted on validation so equal data always
serializes, and hashes, identically.
"""

import hashlib
from datetime import date
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from pulseboard.domain.types import (
    FROZEN,
    AccountId,
    BoardId,
    BoardType,
    EstimationMode,
    Hours,
    IssueId,
    IssueKey,
    SiteId,
    SprintId,
    SprintState,
    StatusCategory,
    UtcDatetime,
    VersionId,
)
from pulseboard.domain.untrusted import UntrustedText

SNAPSHOT_HASH_EXCLUDES = frozenset({"fetched_at"})
"""Volatile fields left out of ``snapshot_hash``, so a refetch of unchanged data matches."""


class BoardRef(BaseModel):
    """A board on a configured site, as parsed from a board link."""

    model_config = FROZEN

    site_id: SiteId
    board_id: BoardId
    project_key: str | None = None


class Person(BaseModel):
    """Someone who can be assigned work."""

    model_config = FROZEN

    account_id: AccountId
    display_name: UntrustedText
    capacity: float = Field(gt=0, le=1)
    holiday_country: str


class Sprint(BaseModel):
    """A Jira sprint."""

    model_config = FROZEN

    sprint_id: SprintId
    name: UntrustedText
    state: SprintState
    start: UtcDatetime | None = None
    end: UtcDatetime | None = None
    goal: UntrustedText | None = None


class Version(BaseModel):
    """A Jira fix version; a milestone when it has a release date (ADR-008)."""

    model_config = FROZEN

    version_id: VersionId
    name: UntrustedText
    release_date: date | None = None
    released: bool = False


class Epic(BaseModel):
    """An epic; a milestone when it has a due date (ADR-008)."""

    model_config = FROZEN

    issue_id: IssueId
    issue_key: IssueKey
    name: UntrustedText
    due_date: date | None = None
    done: bool = False


class BoardColumn(BaseModel):
    """A board column and the Jira status IDs mapped to it."""

    model_config = FROZEN

    name: UntrustedText
    status_ids: tuple[str, ...]


class StatusInfo(BaseModel):
    """A Jira status and its category, so changelog transitions can be classified."""

    model_config = FROZEN

    status_id: str
    name: UntrustedText
    category: StatusCategory


class ExternalBlocker(BaseModel):
    """A blocker that is not on the board(s) in the plan."""

    model_config = FROZEN

    issue_id: IssueId
    issue_key: IssueKey
    status_category: StatusCategory


class Task(BaseModel):
    """One Jira issue, normalized."""

    model_config = FROZEN

    issue_id: IssueId
    issue_key: IssueKey
    summary: UntrustedText
    url: str
    issue_type: UntrustedText
    status_id: str
    status: UntrustedText
    status_category: StatusCategory
    assignee: AccountId | None = None
    board_rank: int = Field(ge=0)
    """Position on the board, 0 = top."""
    priority: UntrustedText | None = None
    story_points: float | None = Field(default=None, ge=0)
    invalid_estimate: bool = False
    """The estimate field held a value that could not be parsed (warning W004)."""
    original_estimate_hours: Hours | None = Field(default=None, ge=0)
    remaining_estimate_hours: Hours | None = Field(default=None, ge=0)
    due_date: date | None = None
    created: UtcDatetime
    resolved: UtcDatetime | None = None
    sprint_id: SprintId | None = None
    epic_issue_id: IssueId | None = None
    fix_version_ids: tuple[VersionId, ...] = ()
    blocked_by: tuple[IssueId, ...] = ()
    """Blockers on the board(s) in the plan."""
    external_blockers: tuple[ExternalBlocker, ...] = ()

    @field_validator("fix_version_ids", "blocked_by", mode="after")
    @classmethod
    def _sorted_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(sorted(set(value)))

    @field_validator("external_blockers", mode="after")
    @classmethod
    def _sorted_blockers(cls, value: tuple[ExternalBlocker, ...]) -> tuple[ExternalBlocker, ...]:
        return tuple(sorted(value, key=lambda b: b.issue_id))


class StatusTransition(BaseModel):
    """A status change from an issue's changelog."""

    model_config = FROZEN

    issue_id: IssueId
    entry_id: int
    """Changelog entry ID; the deduplication key (DESIGN §9.4)."""
    from_status_id: str | None
    to_status_id: str
    at: UtcDatetime


class SprintMembershipChange(BaseModel):
    """An issue added to or removed from a sprint, for burn charts."""

    model_config = FROZEN

    issue_id: IssueId
    entry_id: int
    sprint_id: SprintId
    added: bool
    at: UtcDatetime


class BoardSnapshot(BaseModel):
    """Everything Pulseboard knows about one board at one fetch."""

    model_config = FROZEN

    site_id: SiteId
    board_id: BoardId
    account_id: AccountId
    """The Jira account whose permissions produced this snapshot."""
    board_name: UntrustedText
    board_type: BoardType
    columns: tuple[BoardColumn, ...]
    statuses: tuple[StatusInfo, ...] = ()
    estimation_mode: EstimationMode
    estimation_field: str | None = None
    people: tuple[Person, ...] = ()
    sprints: tuple[Sprint, ...] = ()
    versions: tuple[Version, ...] = ()
    epics: tuple[Epic, ...] = ()
    tasks: tuple[Task, ...] = ()
    transitions: tuple[StatusTransition, ...] = ()
    sprint_changes: tuple[SprintMembershipChange, ...] = ()
    fetched_at: UtcDatetime

    @field_validator("statuses", mode="after")
    @classmethod
    def _sort_statuses(cls, value: tuple[StatusInfo, ...]) -> tuple[StatusInfo, ...]:
        return tuple(sorted(value, key=lambda s: s.status_id))

    @field_validator("people", mode="after")
    @classmethod
    def _sort_people(cls, value: tuple[Person, ...]) -> tuple[Person, ...]:
        return tuple(sorted(value, key=lambda p: p.account_id))

    @field_validator("sprints", mode="after")
    @classmethod
    def _sort_sprints(cls, value: tuple[Sprint, ...]) -> tuple[Sprint, ...]:
        return tuple(sorted(value, key=lambda s: s.sprint_id))

    @field_validator("versions", mode="after")
    @classmethod
    def _sort_versions(cls, value: tuple[Version, ...]) -> tuple[Version, ...]:
        return tuple(sorted(value, key=lambda v: v.version_id))

    @field_validator("epics", mode="after")
    @classmethod
    def _sort_epics(cls, value: tuple[Epic, ...]) -> tuple[Epic, ...]:
        return tuple(sorted(value, key=lambda e: e.issue_id))

    @field_validator("tasks", mode="after")
    @classmethod
    def _sort_tasks(cls, value: tuple[Task, ...]) -> tuple[Task, ...]:
        return tuple(sorted(value, key=lambda t: t.issue_id))

    @field_validator("transitions", mode="after")
    @classmethod
    def _sort_transitions(cls, value: tuple[StatusTransition, ...]) -> tuple[StatusTransition, ...]:
        return tuple(sorted(value, key=lambda t: (t.issue_id, t.at, t.entry_id)))

    @field_validator("sprint_changes", mode="after")
    @classmethod
    def _sort_sprint_changes(
        cls, value: tuple[SprintMembershipChange, ...]
    ) -> tuple[SprintMembershipChange, ...]:
        return tuple(sorted(value, key=lambda c: (c.issue_id, c.at, c.entry_id)))

    @model_validator(mode="after")
    def _unique_identities(self) -> Self:
        checks = (
            ("issue_id", [t.issue_id for t in self.tasks]),
            ("account_id", [p.account_id for p in self.people]),
            ("sprint_id", [s.sprint_id for s in self.sprints]),
            ("version_id", [v.version_id for v in self.versions]),
        )
        for name, ids in checks:
            if len(ids) != len(set(ids)):
                msg = f"Duplicate {name} in snapshot."
                raise ValueError(msg)
        return self

    @property
    def snapshot_hash(self) -> str:
        """SHA-256 of the snapshot content, excluding volatile fields such as ``fetched_at``."""
        content = self.model_dump_json(exclude=set(SNAPSHOT_HASH_EXCLUDES))
        return hashlib.sha256(content.encode()).hexdigest()
