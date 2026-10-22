"""Identity types, unit types, enums and shared field types (DESIGN §7.2)."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, NewType

from pydantic import AfterValidator, AwareDatetime, ConfigDict

# ---------------------------------------------------------------- identity

SiteId = NewType("SiteId", str)
IssueId = NewType("IssueId", int)
"""Jira's numeric issue ID. The identity of an issue; stable when the issue moves project."""
IssueKey = NewType("IssueKey", str)
"""Display only (for example ``ABC-123``). Changes when an issue moves project."""
BoardId = NewType("BoardId", int)
SprintId = NewType("SprintId", int)
VersionId = NewType("VersionId", int)
AccountId = NewType("AccountId", str)
PlanId = NewType("PlanId", str)
ExportId = NewType("ExportId", str)


def jira_name_key(name: str) -> str:
    """How Jira names (link types, statuses, fields) are compared: trimmed, case-insensitive."""
    return name.strip().casefold()


# ---------------------------------------------------------------- units

WorkingDays = NewType("WorkingDays", float)
Hours = NewType("Hours", float)

# ---------------------------------------------------------------- time


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime, normalized to UTC. Naive datetimes are rejected."""

# ---------------------------------------------------------------- model config

FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid")
"""Configuration shared by every domain model."""

# ---------------------------------------------------------------- enums


# ADR-003
class Deployment(StrEnum):
    """Jira deployment type."""

    CLOUD = "cloud"
    DATA_CENTER = "datacenter"
    """Jira Data Center, and Jira Server (treated the same)."""


class AuthMethod(StrEnum):
    """How Pulseboard authenticates to a Jira site (DESIGN §9.1)."""

    API_TOKEN = "api_token"  # noqa: S105 - a method name, not a secret. Cloud: email + token
    OAUTH_3LO = "oauth_3lo"  # Cloud, per-user OAuth; hosted mode only (v1.2)
    PAT = "pat"  # Data Center: personal access token
    ANONYMOUS = "anonymous"  # Data Center public instances: nothing is ever sent


class RequestProfile(StrEnum):
    """How hard Pulseboard may use a site (DESIGN §9.8)."""

    STANDARD = "standard"
    """Private sites the user is authorized for."""
    POLITE = "polite"
    """Public or shared sites: low rate, one request at a time, robots.txt respected."""


class BoardType(StrEnum):
    """Jira agile board type."""

    SCRUM = "scrum"
    KANBAN = "kanban"


class StatusCategory(StrEnum):
    """Jira status category."""

    TO_DO = "to_do"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class SprintState(StrEnum):
    """Jira sprint state."""

    FUTURE = "future"
    ACTIVE = "active"
    CLOSED = "closed"


class EstimationMode(StrEnum):
    """How a board estimates issues."""

    STORY_POINTS = "story_points"
    TIME = "time"
    NONE = "none"


# DESIGN §10.2
class EffortSource(StrEnum):
    """Where a task's effort came from."""

    STORY_POINTS = "story_points"
    ESTIMATE = "estimate"
    REMAINING = "remaining"
    DEFAULT = "default"


# DESIGN §10.5
class ConstraintReason(StrEnum):
    """Why a task starts when it does."""

    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    RELEASE_DATE = "release_date"
    PLAN_DATE = "plan_date"


# DESIGN §3.1
class Confidence(StrEnum):
    """Confidence in a forecast."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ForecastMethod(StrEnum):
    """Which method produced a forecast."""

    SCHEDULE = "schedule"
    MONTE_CARLO = "monte_carlo"


class SolverStatus(StrEnum):
    """Outcome of the scheduling solve."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    NOT_SOLVED = "not_solved"


# DESIGN §12.1
class DetailLevel(StrEnum):
    """Response detail."""

    SUMMARY = "summary"
    STANDARD = "standard"
    FULL = "full"


class WarningLevel(StrEnum):
    """Warning severity."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# DESIGN §10.6
class WarningId(StrEnum):
    """Stable IDs of the registered warning rules."""

    CYCLE = "W001_cycle"
    EXTERNAL_BLOCKER = "W002_external_blocker"
    MISSING_ESTIMATE = "W003_missing_estimate"
    INVALID_ESTIMATE = "W004_invalid_estimate"
    UNASSIGNED = "W005_unassigned"
    DUE_DATE_MISS = "W006_due_date_miss"
    SPRINT_SPILL = "W007_sprint_spill"
    OVERALLOCATION = "W008_overallocation"
    BLOCKED_IN_PROGRESS = "W009_blocked_in_progress"
    MILESTONE_RISK = "W010_milestone_risk"
    LOW_CONFIDENCE = "W011_low_confidence"
    SOLVER_WALLCLOCK_CAP = "W012_solver_wallclock_cap"
    STALE_PLAN = "W013_stale_plan"
    AGING_WIP = "W014_aging_wip"
    UNKNOWN_LINK_TYPE = "W015_unknown_link_type"
    UNKNOWN_PERSON = "W016_unknown_person"
    POINTS_RATE_FALLBACK = "W017_points_rate_fallback"


# DESIGN §3
class ReportId(StrEnum):
    """Reports in the catalogue."""

    STATUS_SUMMARY = "status_summary"
    GANTT = "gantt"
    CRITICAL_PATH = "critical_path"
    MILESTONES = "milestones"
    DEPENDENCY_MAP = "dependency_map"
    WORKLOAD = "workload"
    BURN = "burn"
    CUMULATIVE_FLOW = "cumulative_flow"
    CYCLE_TIME = "cycle_time"
    THROUGHPUT = "throughput"
    AGING_WIP = "aging_wip"
    MONTE_CARLO = "monte_carlo"
    BASELINE_VARIANCE = "baseline_variance"
    WHAT_IF = "what_if"
    PROGRAM_VIEW = "program_view"
    PORTFOLIO_HEALTH = "portfolio_health"
    RAID_LOG = "raid_log"
    FINANCIALS = "financials"
    BENEFITS = "benefits"


# DESIGN §14.2
class ExportFormat(StrEnum):
    """Export formats."""

    HTML = "html"
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"
    PDF = "pdf"
