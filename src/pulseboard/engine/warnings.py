"""Warning-rule registry (DESIGN §10.6, §4.4).

Each rule has a stable ID, a level and its own check over a ``WarningContext``. Rules are
registered as entry points in the ``pulseboard.warnings`` group, so plugins can add rules
without core changes. Rules listed in ``Settings.disabled_warnings`` are skipped.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib.metadata import entry_points

from pulseboard.domain.plan import MilestoneForecast, PlanWarning, ScheduledTask
from pulseboard.domain.snapshot import Sprint, Task
from pulseboard.domain.types import IssueId, SprintId, WarningId, WarningLevel
from pulseboard.engine.effort import Effort
from pulseboard.engine.scheduling import Edge
from pulseboard.engine.workload import WeekLoad

ENTRY_POINT_GROUP = "pulseboard.warnings"
_LEVEL_ORDER = {WarningLevel.ERROR: 0, WarningLevel.WARNING: 1, WarningLevel.INFO: 2}

Finding = tuple[str, tuple[IssueId, ...]]
"""A rule's message and the issues it concerns."""


@dataclass(frozen=True)
class WarningContext:
    """Everything a rule may inspect. Built once per plan."""

    tasks: Mapping[IssueId, Task]
    """Open tasks in the plan."""
    all_tasks: Mapping[IssueId, Task]
    """Every task on the board(s), including done ones."""
    efforts: Mapping[IssueId, Effort]
    scheduled: Mapping[IssueId, ScheduledTask]
    sprints: Mapping[SprintId, Sprint]
    time_zone: str
    dropped_edges: tuple[Edge, ...] = ()
    milestones: tuple[MilestoneForecast, ...] = ()
    workload: tuple[WeekLoad, ...] = ()
    estimated_share: float = 1.0
    estimated_threshold: float = 0.0
    solver_capped: bool = False
    stale_since: datetime | None = None
    aging_outliers: Mapping[IssueId, float] = field(default_factory=dict)
    """In-progress issues older than the cycle-time P85, with their age in days."""
    cycle_time_p85_days: float | None = None
    unknown_link_types: tuple[str, ...] = ()
    """Configured dependency link types that match no link on the board(s)."""
    link_types_seen: tuple[str, ...] = ()
    unknown_people: tuple[str, ...] = ()
    """Configured people or leave entries that match no one on the board(s)."""
    points_fallback_rate: float | None = None
    """Set when story points were converted at the fallback rate."""
    monte_carlo_missing: bool = False
    """No throughput to sample, so there is no Monte Carlo forecast."""

    def key(self, issue_id: IssueId) -> str:
        """Display key for messages (keys are Jira identifiers, never free text)."""
        task = self.all_tasks.get(issue_id)
        return task.issue_key if task else str(issue_id)


@dataclass(frozen=True)
class WarningRule:
    """A registered rule."""

    rule_id: WarningId
    level: WarningLevel
    check: Callable[[WarningContext], Iterable[Finding]]

    def evaluate(self, context: WarningContext) -> list[PlanWarning]:
        """Run the check and wrap each finding as a ``PlanWarning``."""
        return [
            PlanWarning(rule_id=self.rule_id, level=self.level, message=message, issue_ids=ids)
            for message, ids in self.check(context)
        ]


def load_rules() -> tuple[WarningRule, ...]:
    """Every rule registered in the ``pulseboard.warnings`` entry-point group, by rule ID."""
    rules = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        rule = entry.load()
        if not isinstance(rule, WarningRule):
            msg = f"Entry point {entry.name!r} is not a WarningRule."
            raise TypeError(msg)
        rules.append(rule)
    return tuple(sorted(rules, key=lambda r: r.rule_id))


def run_rules(
    rules: Sequence[WarningRule],
    context: WarningContext,
    disabled: Iterable[WarningId] = (),
) -> tuple[PlanWarning, ...]:
    """All warnings from enabled rules, ordered by level, rule ID and issues."""
    skip = set(disabled)
    found = [w for rule in rules if rule.rule_id not in skip for w in rule.evaluate(context)]
    return tuple(sorted(found, key=lambda w: (_LEVEL_ORDER[w.level], w.rule_id, w.issue_ids)))


def week_label(day: date) -> str:
    """ISO week label such as ``2026-W43``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"
