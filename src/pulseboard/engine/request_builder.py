"""Domain → ``ScheduleRequest``: modes, releases, dependencies, cycles and board order (§10.3).

- Assigned tasks get one mode on their assignee. Unassigned tasks get one mode with no person
  ("separate lane") or one mode per eligible person ("auto-assign", tentative).
- In-progress tasks may start on the plan date; future-sprint tasks not before the sprint start.
- Dependencies are links between open tasks on the board(s). Cycles are broken by dropping the
  edge into the lowest-ranked task of each cycle (warning W001).
- In "board order" mode each person's tasks are chained in rank order (in-progress first),
  skipping any edge that would contradict a dependency.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import networkx as nx

from pulseboard.domain.snapshot import BoardSnapshot, Sprint, Task
from pulseboard.domain.types import AccountId, IssueId, SprintId, SprintState, StatusCategory
from pulseboard.engine.calendar import WorkCalendar, local_date
from pulseboard.engine.effort import Effort, open_tasks
from pulseboard.engine.scheduling import (
    Edge,
    Mode,
    Resource,
    ScheduleRequest,
    SchedulerTask,
    SolverBudget,
)
from pulseboard.settings import PriorityMode, TeamSettings, UnassignedPolicy


@dataclass(frozen=True)
class BuiltRequest:
    """A request plus what was changed to make it solvable."""

    request: ScheduleRequest
    dropped_edges: tuple[Edge, ...]
    """Dependency edges dropped to break cycles (W001)."""
    tasks: Mapping[IssueId, Task]
    """The open tasks in the request."""


def merged_open_tasks(snapshots: Sequence[BoardSnapshot]) -> dict[IssueId, Task]:
    """Open tasks across boards, deduplicated by issue ID (first board wins)."""
    merged: dict[IssueId, Task] = {}
    for snapshot in snapshots:
        for task in open_tasks(snapshot.tasks):
            merged.setdefault(task.issue_id, task)
    return dict(sorted(merged.items()))


def _release(task: Task, sprints: Mapping[SprintId, Sprint], cal: WorkCalendar, tz: str) -> int:
    if task.status_category is StatusCategory.IN_PROGRESS or task.sprint_id is None:
        return 0
    sprint = sprints.get(task.sprint_id)
    if sprint is None or sprint.state is not SprintState.FUTURE or sprint.start is None:
        return 0
    return cal.unit_of_date(local_date(sprint.start, tz))


def _rank_key(task: Task) -> tuple[int, int]:
    return (task.board_rank, task.issue_id)


def _break_cycles(
    edges: list[Edge], tasks: Mapping[IssueId, Task]
) -> tuple[list[Edge], tuple[Edge, ...]]:
    graph: nx.DiGraph[IssueId] = nx.DiGraph()
    graph.add_nodes_from(sorted(tasks))
    graph.add_edges_from((e.predecessor, e.successor) for e in edges)
    dropped: list[Edge] = []
    while True:
        try:
            cycle = nx.find_cycle(graph)
        except nx.NetworkXNoCycle:
            break
        into_lowest = max(cycle, key=lambda e: _rank_key(tasks[e[1]]))
        graph.remove_edge(into_lowest[0], into_lowest[1])
        dropped.append(Edge(into_lowest[0], into_lowest[1]))
    kept = [e for e in edges if graph.has_edge(e.predecessor, e.successor)]
    return kept, tuple(sorted(dropped, key=lambda e: (e.predecessor, e.successor)))


def _sequence_edges(tasks: Mapping[IssueId, Task], dependencies: list[Edge]) -> tuple[Edge, ...]:
    graph: nx.DiGraph[IssueId] = nx.DiGraph()
    graph.add_nodes_from(sorted(tasks))
    graph.add_edges_from((e.predecessor, e.successor) for e in dependencies)
    lanes: dict[AccountId, list[Task]] = {}
    for task in tasks.values():
        if task.assignee is not None:
            lanes.setdefault(task.assignee, []).append(task)
    sequence: list[Edge] = []
    for account in sorted(lanes):
        ordered = sorted(
            lanes[account],
            key=lambda t: (t.status_category is not StatusCategory.IN_PROGRESS, *_rank_key(t)),
        )
        for first, second in zip(ordered, ordered[1:], strict=False):
            if nx.has_path(graph, second.issue_id, first.issue_id):
                continue  # rank would contradict a real dependency
            graph.add_edge(first.issue_id, second.issue_id)
            sequence.append(Edge(first.issue_id, second.issue_id))
    return tuple(sequence)


def build_request(
    snapshots: Sequence[BoardSnapshot],
    efforts: Mapping[IssueId, Effort],
    calendar: WorkCalendar,
    team: TeamSettings,
) -> BuiltRequest:
    """Build the solver request for the open work on ``snapshots``."""
    tasks = merged_open_tasks(snapshots)
    sprints = {s.sprint_id: s for snap in snapshots for s in snap.sprints}
    people = sorted(
        {p.account_id for snap in snapshots for p in snap.people}
        | {t.assignee for t in tasks.values() if t.assignee is not None}
    )
    auto_assign = team.unassigned_policy is UnassignedPolicy.AUTO_ASSIGN and bool(people)

    items: list[SchedulerTask] = []
    for task in tasks.values():
        modes: tuple[Mode, ...]
        hours = efforts[task.issue_id].hours
        release = _release(task, sprints, calendar, team.time_zone)
        if task.assignee is not None:
            modes = (Mode(task.assignee, calendar.duration_units(hours, task.assignee)),)
            items.append(SchedulerTask(task.issue_id, modes, release))
        elif auto_assign:
            modes = tuple(Mode(p, calendar.duration_units(hours, p)) for p in people)
            items.append(SchedulerTask(task.issue_id, modes, release, tentative=True))
        else:
            modes = (Mode(None, calendar.duration_units(hours, None)),)
            items.append(SchedulerTask(task.issue_id, modes, release))

    links = sorted(
        {Edge(b, t.issue_id) for t in tasks.values() for b in t.blocked_by if b in tasks},
        key=lambda e: (e.predecessor, e.successor),
    )
    dependencies, dropped = _break_cycles(links, tasks)
    sequence = (
        _sequence_edges(tasks, dependencies)
        if team.priority_mode is PriorityMode.BOARD_ORDER
        else ()
    )
    used = sorted({m.resource for item in items for m in item.modes if m.resource is not None})
    request = ScheduleRequest(
        tasks=tuple(items),
        resources=tuple(Resource(a, calendar.breaks(a)) for a in used),
        dependencies=tuple(dependencies),
        sequence=sequence,
        horizon=calendar.horizon_units,
        budget=SolverBudget(
            deterministic_time=team.solver.deterministic_time,
            wallclock_cap_s=team.solver.wallclock_cap_s,
            seed=team.solver.seed,
        ),
    )
    return BuiltRequest(request=request, dropped_edges=dropped, tasks=tasks)
