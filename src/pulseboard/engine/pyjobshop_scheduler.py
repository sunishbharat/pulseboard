"""``Scheduler`` implementation on PyJobShop and OR-Tools CP-SAT (DESIGN §10.3–§10.4, ADR-019).

Mapping: each person is a machine (capacity 1) with breaks; each task is a job with one task
whose earliest start is its release; each mode is a (person, duration) choice; every edge is an
end-before-start constraint. The objective minimizes the overall finish time first and total
flow time second, so tasks off the critical path start as early as possible instead of floating
to an arbitrary feasible position.
"""

from pyjobshop import Model
from pyjobshop.Result import SolveStatus

from pulseboard.domain.types import AccountId, SolverStatus
from pulseboard.engine.scheduling import ScheduleRequest, ScheduleResult, TaskTiming

_CAP_TOLERANCE = 0.99
"""CP-SAT may stop marginally before its wall-clock limit; within 1% counts as reaching it."""

_STATUS = {
    SolveStatus.OPTIMAL: SolverStatus.OPTIMAL,
    SolveStatus.FEASIBLE: SolverStatus.FEASIBLE,
    SolveStatus.INFEASIBLE: SolverStatus.INFEASIBLE,
}


class PyJobShopScheduler:
    """Deterministic CP-SAT scheduling through PyJobShop."""

    def solve(self, request: ScheduleRequest) -> ScheduleResult:
        """Solve with a single worker, a fixed seed and the deterministic budget."""
        if not request.tasks:
            return ScheduleResult(status=SolverStatus.OPTIMAL, timings=())
        model = Model()
        machines = {
            r.account_id: model.add_machine(breaks=list(r.breaks), name=r.account_id)
            for r in sorted(request.resources, key=lambda r: r.account_id)
        }
        tasks = sorted(request.tasks, key=lambda t: t.issue_id)
        handles = {}
        mode_resources: list[AccountId | None] = []
        for item in tasks:
            job = model.add_job(release_date=item.release, name=str(item.issue_id))
            handle = model.add_task(
                job,
                earliest_start=item.release,
                latest_end=request.horizon,
                allow_breaks=True,
                name=str(item.issue_id),
            )
            handles[item.issue_id] = handle
            for mode in item.modes:
                resources = [] if mode.resource is None else [machines[mode.resource]]
                model.add_mode(handle, resources, mode.duration)
                mode_resources.append(mode.resource)
        for edge in (*request.dependencies, *request.sequence):
            model.add_end_before_start(handles[edge.predecessor], handles[edge.successor])
        model.set_objective(weight_makespan=len(tasks) + 1, weight_total_flow_time=1)

        result = model.solve(
            display=False,
            num_workers=1,
            random_seed=request.budget.seed,
            max_deterministic_time=request.budget.deterministic_time,
            time_limit=request.budget.wallclock_cap_s,
        )
        status = _STATUS.get(result.status, SolverStatus.NOT_SOLVED)
        capped = (
            status is not SolverStatus.OPTIMAL
            and result.runtime >= request.budget.wallclock_cap_s * _CAP_TOLERANCE
        )
        if not result.best.tasks:
            status = (
                SolverStatus.INFEASIBLE
                if status is SolverStatus.INFEASIBLE
                else SolverStatus.NOT_SOLVED
            )
            return ScheduleResult(status=status, timings=(), wallclock_capped=capped)
        if status is SolverStatus.NOT_SOLVED:
            status = SolverStatus.FEASIBLE  # stopped by a limit with a solution in hand
        timings = tuple(
            TaskTiming(
                issue_id=item.issue_id,
                start=solved.start,
                end=solved.end,
                resource=mode_resources[solved.mode],
            )
            for item, solved in zip(tasks, result.best.tasks, strict=True)
        )
        return ScheduleResult(status=status, timings=timings, wallclock_capped=capped)
