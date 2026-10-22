"""Float, tight edges, critical path and constraint reasons (DESIGN §10.5).

- Precedence graph: dependency edges plus "same person, consecutive task" edges taken from the
  solved schedule, held fixed.
- Earliest start = solved start. Latest start comes from a backward pass in reverse topological
  order from the plan end. Total float = latest − earliest start, in half-day units.
- A task is critical when its total float is zero. An edge is tight when the predecessor's end
  equals the successor's start.
- A critical path is a chain of critical tasks linked by tight edges that ends at the plan end.
  Paths are found longest first (span, then task count, then IDs) by a best-first search, so
  the ``max_paths`` cap only ever drops shorter paths, and says when it did (ADDENDUM-03 §7).
"""

import heapq
from collections.abc import Mapping
from dataclasses import dataclass

import networkx as nx

from pulseboard.domain.types import AccountId, ConstraintReason, IssueId
from pulseboard.engine.scheduling import ScheduleRequest, ScheduleResult, TaskTiming


@dataclass(frozen=True)
class TaskAnalysis:
    """Analysis of one scheduled task."""

    timing: TaskTiming
    total_float: int
    """Half-day units."""
    critical: bool
    constraint_reason: ConstraintReason
    constraining_issue_id: IssueId | None


@dataclass(frozen=True)
class Analysis:
    """Analysis of a whole schedule."""

    tasks: Mapping[IssueId, TaskAnalysis]
    critical_paths: tuple[tuple[IssueId, ...], ...]
    """Longest first."""
    plan_end: int
    """Exclusive end unit of the last task; 0 for an empty schedule."""
    paths_truncated: bool = False
    """More critical paths exist than the ``max_paths`` cap kept."""


def _resource_edges(timings: Mapping[IssueId, TaskTiming]) -> set[tuple[IssueId, IssueId]]:
    lanes: dict[AccountId, list[TaskTiming]] = {}
    for timing in timings.values():
        if timing.resource is not None:
            lanes.setdefault(timing.resource, []).append(timing)
    edges: set[tuple[IssueId, IssueId]] = set()
    for lane in lanes.values():
        ordered = sorted(lane, key=lambda t: (t.start, t.issue_id))
        edges.update((a.issue_id, b.issue_id) for a, b in zip(ordered, ordered[1:], strict=False))
    return edges


def _latest_starts(
    graph: "nx.DiGraph[IssueId]", timings: Mapping[IssueId, TaskTiming], plan_end: int
) -> dict[IssueId, int]:
    latest: dict[IssueId, int] = {}
    for node in reversed(list(nx.lexicographical_topological_sort(graph))):
        finish = min((latest[s] for s in graph.successors(node)), default=plan_end)
        timing = timings[node]
        latest[node] = finish - (timing.end - timing.start)
    return latest


def _critical_paths(
    graph: "nx.DiGraph[IssueId]",
    timings: Mapping[IssueId, TaskTiming],
    critical: set[IssueId],
    plan_end: int,
    limit: int,
) -> tuple[tuple[tuple[IssueId, ...], ...], bool]:
    """The ``limit`` longest critical paths, longest first, and whether more exist.

    Every path ends at the plan end, so its span is set by its first task. A best-first search
    orders partial paths by (span, longest possible task count, IDs); no completion of a partial
    path sorts before it, so paths come out in their final order and the cap cuts only the tail.
    """
    tight: nx.DiGraph[IssueId] = nx.DiGraph()
    tight.add_nodes_from(sorted(critical))
    tight.add_edges_from(
        (a, b)
        for a, b in graph.edges
        if a in critical and b in critical and timings[a].end == timings[b].start
    )
    # Most tasks on a path from each node to the plan end (absent: no such path).
    remaining: dict[IssueId, int] = {}
    for node in reversed(list(nx.lexicographical_topological_sort(tight))):
        if timings[node].end == plan_end:
            remaining[node] = 1
        else:
            reachable = [remaining[s] for s in tight.successors(node) if s in remaining]
            if reachable:
                remaining[node] = 1 + max(reachable)

    heap: list[tuple[int, int, tuple[IssueId, ...]]] = [
        (timings[s].start, -remaining[s], (s,))
        for s in tight
        if tight.in_degree(s) == 0 and s in remaining
    ]
    heapq.heapify(heap)
    paths: list[tuple[IssueId, ...]] = []
    while heap and len(paths) < limit:
        start, potential, path = heapq.heappop(heap)
        last = path[-1]
        if timings[last].end == plan_end:
            paths.append(path)
            continue
        for successor in tight.successors(last):
            if successor in remaining:
                ahead = len(path) + remaining[successor]
                heapq.heappush(heap, (start, -ahead, (*path, successor)))
    return tuple(paths), bool(heap)


def _reason(
    timing: TaskTiming,
    release: int,
    graph: "nx.DiGraph[IssueId]",
    timings: Mapping[IssueId, TaskTiming],
    dependencies: set[tuple[IssueId, IssueId]],
) -> tuple[ConstraintReason, IssueId | None]:
    tight = sorted(p for p in graph.predecessors(timing.issue_id) if timings[p].end == timing.start)
    for predecessor in tight:
        if (predecessor, timing.issue_id) in dependencies:
            return ConstraintReason.DEPENDENCY, predecessor
    if tight:
        return ConstraintReason.RESOURCE, tight[0]
    if timing.start == release and release > 0:
        return ConstraintReason.RELEASE_DATE, None
    if timing.start == 0:
        return ConstraintReason.PLAN_DATE, None
    # Started later than every predecessor and its release: the person was unavailable.
    return (ConstraintReason.RESOURCE if timing.resource else ConstraintReason.PLAN_DATE), None


def analyze(request: ScheduleRequest, result: ScheduleResult, max_paths: int) -> Analysis:
    """Analyze a solved schedule."""
    timings = {t.issue_id: t for t in result.timings}
    if not timings:
        return Analysis(tasks={}, critical_paths=(), plan_end=0)
    dependencies = {
        (e.predecessor, e.successor)
        for e in request.dependencies
        if e.predecessor in timings and e.successor in timings
    }
    graph: nx.DiGraph[IssueId] = nx.DiGraph()
    graph.add_nodes_from(sorted(timings))
    graph.add_edges_from(sorted(dependencies | _resource_edges(timings)))
    plan_end = max(t.end for t in timings.values())
    latest = _latest_starts(graph, timings, plan_end)
    releases = {t.issue_id: t.release for t in request.tasks}

    analyses: dict[IssueId, TaskAnalysis] = {}
    for issue_id, timing in sorted(timings.items()):
        total_float = max(latest[issue_id] - timing.start, 0)
        reason, constraining = _reason(
            timing, releases.get(issue_id, 0), graph, timings, dependencies
        )
        analyses[issue_id] = TaskAnalysis(
            timing, total_float, total_float == 0, reason, constraining
        )
    critical = {i for i, a in analyses.items() if a.critical}
    paths, truncated = _critical_paths(graph, timings, critical, plan_end, max_paths)
    return Analysis(
        tasks=analyses, critical_paths=paths, plan_end=plan_end, paths_truncated=truncated
    )
