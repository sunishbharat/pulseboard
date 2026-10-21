"""The ``Scheduler`` protocol and its solver-neutral request and result (DESIGN §10.3).

Times are units on the WorkCalendar axis (team working half-days from the plan date).
"""

from dataclasses import dataclass
from typing import Protocol

from pulseboard.domain.types import AccountId, IssueId, SolverStatus
from pulseboard.engine.calendar import Break


@dataclass(frozen=True)
class Mode:
    """One way to perform a task: on a person (or no one) for a duration."""

    resource: AccountId | None
    duration: int


@dataclass(frozen=True)
class SchedulerTask:
    """A task to place on the axis."""

    issue_id: IssueId
    modes: tuple[Mode, ...]
    release: int = 0
    """Earliest start unit (plan date or future sprint start)."""
    tentative: bool = False
    """The solver chooses the person (auto-assign)."""


@dataclass(frozen=True)
class Resource:
    """A person; capacity 1, with breaks for their own non-working time."""

    account_id: AccountId
    breaks: tuple[Break, ...] = ()


@dataclass(frozen=True)
class Edge:
    """``successor`` starts no earlier than ``predecessor`` ends."""

    predecessor: IssueId
    successor: IssueId


@dataclass(frozen=True)
class SolverBudget:
    """Deterministic budget (DESIGN §10.4, ADR-019)."""

    deterministic_time: float
    wallclock_cap_s: float
    seed: int


@dataclass(frozen=True)
class ScheduleRequest:
    """Everything a scheduler needs; no Jira concepts."""

    tasks: tuple[SchedulerTask, ...]
    resources: tuple[Resource, ...]
    dependencies: tuple[Edge, ...]
    """Real dependencies (issue links)."""
    sequence: tuple[Edge, ...]
    """Board-order edges between a person's consecutive tasks."""
    horizon: int
    budget: SolverBudget


@dataclass(frozen=True)
class TaskTiming:
    """Where the solver placed a task."""

    issue_id: IssueId
    start: int
    end: int
    """Exclusive end unit."""
    resource: AccountId | None


@dataclass(frozen=True)
class ScheduleResult:
    """The solver outcome. ``timings`` is sorted by issue ID and empty if no solution exists."""

    status: SolverStatus
    timings: tuple[TaskTiming, ...]
    wallclock_capped: bool = False
    """The wall-clock safety cap stopped the solver (warning W012)."""


class Scheduler(Protocol):
    """Solves a ``ScheduleRequest``. Implementations must be deterministic."""

    def solve(self, request: ScheduleRequest) -> ScheduleResult:
        """Place every task, or report that no solution was found."""
        ...
