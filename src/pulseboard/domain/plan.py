"""Plan, forecast, diff and export models (DESIGN §8, §11.5)."""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from pulseboard.domain.types import (
    FROZEN,
    AccountId,
    BoardId,
    Confidence,
    ConstraintReason,
    EffortSource,
    ExportFormat,
    ExportId,
    ForecastMethod,
    Hours,
    IssueId,
    IssueKey,
    PlanId,
    ReportId,
    SiteId,
    SolverStatus,
    StatusCategory,
    UtcDatetime,
    WarningId,
    WarningLevel,
    WorkingDays,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.timezones import TimeZoneName

PLAN_SCHEMA_VERSION = "1.0.0"
"""SemVer of the plan schema (ADR-011)."""


class ScheduledTask(BaseModel):
    """A task with its solved dates and analysis."""

    model_config = FROZEN

    issue_id: IssueId
    issue_key: IssueKey
    start: date
    end: date
    effort_hours: Hours = Field(ge=0)
    duration_working_days: WorkingDays = Field(ge=0)
    effort_source: EffortSource
    lane: AccountId | None
    """The assignee's account ID; ``None`` for the unassigned lane."""
    constraint_reason: ConstraintReason
    constraining_issue_id: IssueId | None = None
    total_float_working_days: WorkingDays = Field(ge=0)
    critical: bool
    tentative: bool = False
    """True when the solver chose the assignee (auto-assign)."""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            msg = "A scheduled task cannot end before it starts."
            raise ValueError(msg)
        return self


class DependencyEdge(BaseModel):
    """``successor`` cannot start until ``predecessor`` ends."""

    model_config = FROZEN

    predecessor: IssueId
    successor: IssueId


class Forecast(BaseModel):
    """A completion forecast with percentiles (DESIGN §10.7)."""

    model_config = FROZEN

    method: ForecastMethod
    p50: date
    p85: date
    p95: date
    trial_count: int | None = Field(default=None, gt=0)
    seed: int | None = None
    history_weeks: int | None = Field(default=None, gt=0)
    confidence: Confidence
    confidence_reason: str

    @model_validator(mode="after")
    def _percentiles_ordered(self) -> Self:
        if not self.p50 <= self.p85 <= self.p95:
            msg = "Forecast percentiles must satisfy P50 <= P85 <= P95."
            raise ValueError(msg)
        return self


# ADR-008
class MilestoneKind(StrEnum):
    """What a milestone is in Jira."""

    FIX_VERSION = "fix_version"
    EPIC = "epic"


class MilestoneForecast(BaseModel):
    """A milestone's forecast against its target date."""

    model_config = FROZEN

    kind: MilestoneKind
    milestone_id: int
    """Version ID for a fix version, issue ID for an epic."""
    name: UntrustedText
    target_date: date | None
    forecast_date: date | None
    slip_working_days: WorkingDays | None
    """Positive when late."""


class PlanWarning(BaseModel):
    """A registered warning raised while building a plan."""

    model_config = FROZEN

    rule_id: WarningId
    level: WarningLevel
    message: str
    issue_ids: tuple[IssueId, ...] = ()


class Plan(BaseModel):
    """An immutable plan, identified by ``plan_id`` (DESIGN §11.1)."""

    model_config = FROZEN

    schema_version: str = PLAN_SCHEMA_VERSION
    plan_id: PlanId
    site_id: SiteId
    board_ids: tuple[BoardId, ...] = Field(min_length=1)
    account_id: AccountId
    plan_date: date
    team_time_zone: TimeZoneName
    engine_version: str
    settings_hash: str
    snapshot_hash: str
    scheduled_tasks: tuple[ScheduledTask, ...]
    dependency_edges: tuple[DependencyEdge, ...]
    critical_paths: tuple[tuple[IssueId, ...], ...]
    """Every critical chain, longest first (DESIGN §10.5)."""
    forecast: Forecast
    """The primary projection (DESIGN §3.1)."""
    secondary_forecast: Forecast | None = None
    """The other projection, shown alongside the primary one."""
    estimated_share: float = Field(default=1.0, ge=0, le=1)
    """Share of open issues with a real estimate."""
    milestones: tuple[MilestoneForecast, ...]
    warnings: tuple[PlanWarning, ...]
    solver_status: SolverStatus
    generated_at: UtcDatetime
    stale_since: UtcDatetime | None = None


# ---------------------------------------------------------------- plan diff


class DateMove(BaseModel):
    """A task whose solved dates moved between two plans."""

    model_config = FROZEN

    issue_id: IssueId
    old_start: date
    new_start: date
    old_end: date
    new_end: date
    end_moved_working_days: WorkingDays


class Reassignment(BaseModel):
    """A task assigned to someone else."""

    model_config = FROZEN

    issue_id: IssueId
    old_assignee: AccountId | None
    new_assignee: AccountId | None


class StatusChange(BaseModel):
    """A task whose status category changed."""

    model_config = FROZEN

    issue_id: IssueId
    old: StatusCategory
    new: StatusCategory


class EstimateChange(BaseModel):
    """A task whose effort changed."""

    model_config = FROZEN

    issue_id: IssueId
    old_effort_hours: Hours
    new_effort_hours: Hours


class PlanDiff(BaseModel):
    """Differences between two plans, keyed by issue ID (DESIGN §11.5)."""

    model_config = FROZEN

    from_plan_id: PlanId
    to_plan_id: PlanId
    added: tuple[IssueId, ...] = ()
    removed: tuple[IssueId, ...] = ()
    moved: tuple[DateMove, ...] = ()
    reassigned: tuple[Reassignment, ...] = ()
    status_changed: tuple[StatusChange, ...] = ()
    estimate_changed: tuple[EstimateChange, ...] = ()
    forecast_end_moved_working_days: WorkingDays
    critical_path_added: tuple[IssueId, ...] = ()
    critical_path_removed: tuple[IssueId, ...] = ()


# ---------------------------------------------------------------- exports


class ExportRecord(BaseModel):
    """Metadata for a rendered export (DESIGN §12.3)."""

    model_config = FROZEN

    export_id: ExportId
    site_id: SiteId
    account_id: AccountId
    report_id: ReportId
    format: ExportFormat
    media_type: str
    size_bytes: int = Field(ge=0)
    created: UtcDatetime
    expires: UtcDatetime
    resource_uri: str

    @model_validator(mode="after")
    def _expires_after_created(self) -> Self:
        if self.expires <= self.created:
            msg = "An export must expire after it is created."
            raise ValueError(msg)
        return self
