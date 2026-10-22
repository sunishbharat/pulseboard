"""Tool outputs: plain Pydantic models, kept small (DESIGN §12.1, §13.3).

Jira free text appears only in ``UntrustedText`` fields.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from pulseboard.domain.plan import MilestoneKind
from pulseboard.domain.types import (
    Confidence,
    ConstraintReason,
    Deployment,
    ForecastMethod,
    SolverStatus,
    WarningId,
    WarningLevel,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.mcp.pagination import Page

_OUT = ConfigDict(frozen=True)


class SiteOut(BaseModel):
    """A configured Jira site."""

    model_config = _OUT
    site_id: str
    base_url: str
    deployment: Deployment


class SitesOut(BaseModel):
    """Configured sites; board links must be on one of them."""

    model_config = _OUT
    sites: list[SiteOut]


class WarningOut(BaseModel):
    """A plan warning; ``issues`` are issue keys."""

    model_config = _OUT
    rule: WarningId
    level: WarningLevel
    message: str
    issues: list[str] = []


class MilestoneOut(BaseModel):
    """A milestone against its target; ``slip_days`` in working days (positive = late)."""

    model_config = _OUT
    kind: MilestoneKind
    id: int
    name: UntrustedText | None
    target: date | None
    forecast: date | None
    slip_days: float | None


class FlowSummary(BaseModel):
    """Flow metrics; throughput oldest week first."""

    model_config = _OUT
    window_days: int
    throughput_per_week: list[int]
    cycle_time_p50_days: float | None
    cycle_time_p85_days: float | None
    aging_outliers: list[str]
    """Issue keys in progress longer than the cycle-time P85."""
    note: str | None = None
    """Why a metric is missing or a list is empty."""


class BuildPlanOut(BaseModel):
    """A plan handle and its headline numbers."""

    model_config = _OUT
    plan_id: str | None
    forecast_p50: date | None = None
    forecast_p85: date | None = None
    method: ForecastMethod | None = None
    confidence: Confidence | None = None
    confidence_reason: str | None = None
    solver_status: SolverStatus | None = None
    open_issues: int
    critical_issues: int = 0
    warning_count: int = 0
    top_warnings: list[WarningOut] = []
    milestones_at_risk: list[MilestoneOut] = []
    stale_since: datetime | None = None
    partial_flow: FlowSummary | None = None
    """Set when the build timed out: flow metrics only, no plan."""
    partial_reason: str | None = None
    """Why ``plan_id`` is null (for example ``timeout: …``) and what to do next."""


class PlanSummaryOut(BaseModel):
    """Status summary of a plan."""

    model_config = _OUT
    plan_id: str
    plan_date: date
    to_do: int
    in_progress: int
    done: int
    blocked: int
    forecast_p50: date
    forecast_p85: date
    """The whole plan's forecast end."""
    method: ForecastMethod
    confidence: Confidence
    last_milestone: MilestoneOut | None
    """The milestone with the latest target, with its own forecast and slip."""
    top_warnings: list[WarningOut]
    milestones_at_risk: list[MilestoneOut]


class CriticalTaskOut(BaseModel):
    """A task on the critical path."""

    model_config = _OUT
    key: str
    summary: UntrustedText | None = None
    start: date
    end: date
    assignee: str | None
    reason: ConstraintReason
    after: str | None = None
    """Key of the task it waits for."""


class CriticalPathOut(BaseModel):
    """The longest critical path as a page of tasks; paths counts every critical path."""

    model_config = _OUT
    paths: int
    paths_truncated: bool
    """More paths exist than ``limits.max_critical_paths`` kept; the longest are kept."""
    page: Page[CriticalTaskOut]


class WarningsOut(BaseModel):
    """Warning counts by rule and a page of warnings."""

    model_config = _OUT
    counts: dict[str, int]
    page: Page[WarningOut]


class LoadOut(BaseModel):
    """One person-week of load, in hours."""

    model_config = _OUT
    account_id: str
    week: str
    allocated_h: float
    available_h: float
    utilization: float


class ForecastLine(BaseModel):
    """Completion dates for one forecast method."""

    model_config = _OUT
    method: ForecastMethod
    p50: date
    p85: date
    p95: date
    confidence: Confidence
    reason: str


class ForecastOut(BaseModel):
    """Forecast for a scope; ``alternative`` is the other method, if available."""

    model_config = _OUT
    scope: str
    primary: ForecastLine
    alternative: ForecastLine | None = None


class RenderOut(BaseModel):
    """A rendered export, available as an MCP resource."""

    model_config = _OUT
    uri: str
    media_type: str
    size_bytes: int
    summary: list[str]


class LinkedRenderOut(RenderOut):
    """A rendered export with a short-lived download link (hosted HTTP mode)."""

    download_url: str
    """Signed, valid for 15 minutes and only on the instance that rendered it."""
