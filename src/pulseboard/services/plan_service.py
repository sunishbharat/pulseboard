"""PlanService: orchestration, plan identity, caching, stale plans, progress and timeouts.

Data flow (DESIGN §9.6): validate the link → refresh the snapshot → compute ``plan_id`` → reuse a
stored plan or build one → store it. Sync Jira calls and the solver run in worker threads; the
whole build runs under the tool timeout (DESIGN §12.4).

A ``plan_id`` is self-contained (ADDENDUM-03 §9): follow-up answers come from the stored plan and
the snapshots it was built from. If those snapshots are gone, or the settings changed since the
plan was built, the plan is expired: no answer is ever computed from other inputs.
"""

import hashlib
import json
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import anyio
import anyio.from_thread
import anyio.to_thread
from cachetools import LRUCache

from pulseboard.domain.plan import Forecast, MilestoneKind, Plan, PlanWarning
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    IssueId,
    PlanId,
    SiteId,
    SolverStatus,
    StatusCategory,
    WarningId,
)
from pulseboard.engine.calendar import calendar_from_settings, local_date
from pulseboard.engine.effort import open_tasks
from pulseboard.engine.flow import FlowMetrics, flow_metrics
from pulseboard.engine.forecaster import (
    Throughput,
    choose_primary,
    monte_carlo_forecast,
    seed_for,
    weekly_throughput,
)
from pulseboard.engine.planner import (
    PlanRequest,
    Stages,
    build_plan,
    combined_snapshot_hash,
    people_of,
    schedule_forecast,
)
from pulseboard.engine.scheduling import Scheduler
from pulseboard.engine.warnings import WarningContext, WarningRule, run_rules, week_label
from pulseboard.engine.workload import WeekLoad, plan_workload
from pulseboard.errors import (
    DURABLE_STORAGE_MESSAGE,
    BoardTooLargeError,
    InvalidArgumentError,
    JiraUnavailableError,
    PlanExpiredError,
    ServerBusyError,
    SiteNotConfiguredError,
    ToolTimeoutError,
)
from pulseboard.links import parse_board_link
from pulseboard.services.refresh import BoardRefresher
from pulseboard.services.solve_slots import SolveSlots
from pulseboard.settings import Settings
from pulseboard.sites import Site, SiteRegistry
from pulseboard.sources.base import BoardSource
from pulseboard.store.protocol import Store

ProgressCallback = Callable[[float, float | None, str], Coroutine[object, object, None]]
"""(progress, total, message), forwarded to the MCP progress mechanism."""
Owner = Callable[[], AccountId]
"""The calling Jira account (hosted mode); plans of other accounts are never served."""

__all__ = [
    "BuildResult",
    "FlowMetrics",
    "PartialResult",
    "PlanService",
    "Throughput",
    "WeekLoad",
    "settings_hash",
    "week_label",
]
"""Engine result types are re-exported for interfaces, which never import the engine."""

_PLAN_ID_LENGTH = 32
_MEMO_SIZE = 32
EXPIRED_HINT = "Call pb_build_plan again."


@dataclass(frozen=True)
class PartialResult:
    """What could be computed before the timeout: flow metrics without the schedule."""

    site_id: SiteId
    board_id: BoardId
    open_issues: int
    flow: FlowMetrics
    throughput: Throughput
    keys: dict[IssueId, str]
    reason: str
    """Why there is no plan, and what to do next."""


@dataclass(frozen=True)
class BuildResult:
    """The outcome of ``build``: a plan, or a partial result after a timeout."""

    plan: Plan | None
    partial: PartialResult | None = None
    cached: bool = False
    """The plan was already stored for this plan ID."""


def settings_hash(settings: Settings) -> str:
    """Hash of the settings that change plan results (team, limits, disabled warnings)."""
    relevant = settings.model_dump(mode="json", include={"team", "limits", "disabled_warnings"})
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def compute_plan_id(
    *,
    site_id: SiteId,
    account_id: AccountId,
    board_ids: Sequence[BoardId],
    snapshot_hash: str,
    settings_digest: str,
    plan_date: str,
    time_zone: str,
    engine_version: str,
) -> PlanId:
    """``plan_id`` per DESIGN §11.1: any change to its inputs gives a new plan."""
    parts = [
        site_id,
        account_id,
        ",".join(str(b) for b in sorted(board_ids)),
        snapshot_hash,
        settings_digest,
        plan_date,
        time_zone,
        engine_version,
    ]
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return PlanId(digest[:_PLAN_ID_LENGTH])


def _require_schedule(stages: Stages) -> None:
    """Refuse a plan whose open work the solver could not place: its forecast would be a guess.

    Raises:
        BoardTooLargeError: the solver proved the work cannot finish within the planning horizon.
        ToolTimeoutError: the solver budget ran out before it found any schedule.
    """
    if stages.result.timings or not stages.built.tasks:
        return
    if stages.result.status is SolverStatus.INFEASIBLE:
        last = stages.calendar.last_day.isoformat()
        msg = f"The open work does not fit in the planning horizon (until {last})."
        hint = "Use a board with a narrower filter or raise limits.planning_horizon_years."
        raise BoardTooLargeError(msg, hint=hint)
    msg = "The scheduler found no plan within its budget."
    hint = "Use a board with a narrower filter or raise team.solver.deterministic_time."
    raise ToolTimeoutError(msg, hint=hint)


class PlanService:
    """Builds, stores and serves plans."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: SiteRegistry,
        store: Store,
        source_for: Callable[[Site], BoardSource],
        scheduler: Scheduler,
        rules: Sequence[WarningRule],
        clock: Callable[[], datetime],
        engine_version: str,
        memoize: bool = True,
        owner: Owner | None = None,
        reuse_sources: bool = True,
        stale_plans: bool = True,
        solve_slots: SolveSlots | None = None,
    ) -> None:
        """Local mode uses the defaults. Hosted Profile A passes ``owner`` (the caller's account),
        ``reuse_sources=False`` (each call carries its own credentials) and ``stale_plans=False``
        (no durable storage to fall back on). ``solve_slots`` caps concurrent solves; a build
        that waits for a slot for the whole tool timeout fails with ``server_busy``."""
        self._settings = settings
        self._registry = registry
        self._store = store
        self._source_for = source_for
        self._sources: dict[SiteId, BoardSource] = {}
        self._scheduler = scheduler
        self._rules = tuple(rules)
        self._clock = clock
        self._engine_version = engine_version
        self._site_settings: dict[SiteId, tuple[Settings, str]] = {}
        self._memo: LRUCache[PlanId, Plan] | None = (
            LRUCache(maxsize=_MEMO_SIZE) if memoize else None
        )
        self._owner = owner
        self._reuse_sources = reuse_sources
        self._stale_plans = stale_plans
        self._solve_slots = solve_slots

    @property
    def registry(self) -> SiteRegistry:
        """The configured sites."""
        return self._registry

    def _for_site(self, site_id: SiteId) -> tuple[Settings, str]:
        if site_id not in self._site_settings:
            try:
                site_settings = self._settings.for_site(self._registry.get(site_id))
            except SiteNotConfiguredError:  # plans of a site that was since removed
                site_settings = self._settings
            self._site_settings[site_id] = (site_settings, settings_hash(site_settings))
        return self._site_settings[site_id]

    def settings_for(self, site_id: SiteId) -> Settings:
        """The settings with ``site_id``'s request-profile limits applied (DESIGN §9.8)."""
        return self._for_site(site_id)[0]

    def _refresher(self, site_id: SiteId) -> BoardRefresher:
        limits = self.settings_for(site_id).limits
        return BoardRefresher(self._store, limits, self._settings.team.time_zone, self._clock)

    def _source(self, site: Site) -> BoardSource:
        if not self._reuse_sources:
            return self._source_for(site)
        if site.site_id not in self._sources:
            self._sources[site.site_id] = self._source_for(site)
        return self._sources[site.site_id]

    # ------------------------------------------------------------ lookup

    def get_plan(self, plan_id: PlanId) -> Plan:
        """A stored plan, built with the settings in effect now.

        Raises:
            PlanExpiredError: if the plan ID is unknown, was purged, or (hosted mode) belongs to
                another account. The message is the same, so plan IDs of others reveal nothing.
                Also if the settings changed since the plan was built: follow-up tools would
                otherwise answer with other settings than the plan's (ADDENDUM-03 §9).
        """
        plan = self._memo.get(plan_id) if self._memo is not None else None
        if plan is None:
            plan = self._store.get_plan(plan_id)
        if plan is None or (self._owner is not None and plan.account_id != self._owner()):
            msg = "This plan is unknown or has expired."
            raise PlanExpiredError(msg, hint=EXPIRED_HINT)
        if plan.settings_hash != self._for_site(plan.site_id)[1]:
            msg = "The settings changed since this plan was built."
            raise PlanExpiredError(msg, hint=EXPIRED_HINT)
        self._remember(plan)
        return plan

    def snapshots_for(self, plan: Plan) -> list[BoardSnapshot]:
        """The snapshots a plan was built from, one per board.

        Raises:
            PlanExpiredError: a snapshot is gone (purged, or evicted in hosted mode); answering
                without it would report empty or zero data (ADDENDUM-03 §9).
        """
        found = []
        for board in plan.board_ids:
            snap = self._store.snapshot_by_hash(
                plan.site_id, plan.account_id, board, plan.snapshot_hash
            )
            if snap is None:
                msg = "The board data this plan was built from is gone."
                raise PlanExpiredError(msg, hint=EXPIRED_HINT)
            found.append(snap)
        return found

    def _remember(self, plan: Plan) -> None:
        if self._memo is not None:
            self._memo[plan.plan_id] = plan

    # ------------------------------------------------------------ build

    async def build(self, board_link: str, progress: ProgressCallback | None = None) -> BuildResult:
        """Build (or reuse) the plan for a board link, within the tool timeout.

        Raises:
            SiteNotConfiguredError, InvalidBoardLinkError: before any network call.
            BoardTooLargeError, AuthFailedError, PermissionDeniedError, BoardNotFoundError,
            RateLimitedError: from Jira.
            BoardTooLargeError: the open work does not fit in the planning horizon.
            ToolTimeoutError: the solver found no schedule within its budget.
            JiraUnavailableError: Jira is unreachable and no plan is recent enough to serve.
            ToolTimeoutError: the timeout passed before any snapshot was available.
            ServerBusyError: the timeout passed while waiting for a solver slot.
        """
        ref = parse_board_link(board_link, self._registry)
        site = self._registry.get(ref.site_id)
        snapshot: BoardSnapshot | None = None

        def report(stage: str, done: int, total: int | None) -> None:
            if progress is not None:
                anyio.from_thread.run(
                    progress, float(done), None if total is None else float(total), stage
                )

        waiting_for_slot = False
        with anyio.move_on_after(self._settings.limits.tool_timeout_s) as scope:
            try:
                refreshed = await anyio.to_thread.run_sync(
                    lambda: self._refresher(site.site_id).refresh(
                        self._source(site), site.site_id, ref.board_id, report
                    ),
                    abandon_on_cancel=True,
                )
            except JiraUnavailableError:
                if not self._stale_plans:
                    msg = f"Jira is unreachable. {DURABLE_STORAGE_MESSAGE}"
                    raise JiraUnavailableError(msg, hint="Retry later.") from None
                return BuildResult(plan=self._stale_plan(site.site_id, ref.board_id))
            snapshot = refreshed.snapshot
            self._check_size(snapshot)
            plan_id = self._plan_id(snapshot)
            existing = self._lookup(plan_id)
            if existing is not None:
                return BuildResult(plan=existing, cached=True)
            if progress is not None:
                await progress(0.0, None, "solving")
            request = self._plan_request(snapshot, plan_id)

            def solve() -> tuple[Plan, Stages]:
                plan, stages = build_plan(request, self._scheduler, self._rules)
                _require_schedule(stages)
                return plan, stages

            if self._solve_slots is None:
                plan, _ = await anyio.to_thread.run_sync(solve, abandon_on_cancel=True)
            else:
                waiting_for_slot = True
                async with self._solve_slots.reserve() as slot:
                    waiting_for_slot = False
                    plan, _ = await slot.run(solve)
            self._store.save_plan(plan)
            self._remember(plan)
            if progress is not None:
                await progress(1.0, 1.0, "done")
            return BuildResult(plan=plan)

        if scope.cancelled_caught and waiting_for_slot:
            raise ServerBusyError
        if scope.cancelled_caught and snapshot is not None:
            return BuildResult(plan=None, partial=self._partial(snapshot))
        msg = "Building the plan took too long."
        raise ToolTimeoutError(msg, hint="Retry later or raise limits.tool_timeout_s.")

    # ------------------------------------------------------------ helpers

    def _plan_date_and_id(self, snapshot: BoardSnapshot) -> tuple[str, PlanId]:
        tz = self._settings.team.time_zone
        plan_date = local_date(self._clock(), tz).isoformat()
        plan_id = compute_plan_id(
            site_id=snapshot.site_id,
            account_id=snapshot.account_id,
            board_ids=(snapshot.board_id,),
            snapshot_hash=combined_snapshot_hash([snapshot]),
            settings_digest=self._for_site(snapshot.site_id)[1],
            plan_date=plan_date,
            time_zone=tz,
            engine_version=self._engine_version,
        )
        return plan_date, plan_id

    def _plan_id(self, snapshot: BoardSnapshot) -> PlanId:
        return self._plan_date_and_id(snapshot)[1]

    def _lookup(self, plan_id: PlanId) -> Plan | None:
        try:
            return self.get_plan(plan_id)
        except PlanExpiredError:
            return None

    def _plan_request(self, snapshot: BoardSnapshot, plan_id: PlanId) -> PlanRequest:
        plan_date, _ = self._plan_date_and_id(snapshot)
        site_settings, digest = self._for_site(snapshot.site_id)
        return PlanRequest(
            snapshots=(snapshot,),
            settings=site_settings,
            plan_date=date.fromisoformat(plan_date),
            plan_id=plan_id,
            engine_version=self._engine_version,
            settings_hash=digest,
            generated_at=self._clock(),
            seed=seed_for(plan_id),
        )

    def _check_size(self, snapshot: BoardSnapshot) -> None:
        limit = self.settings_for(snapshot.site_id).limits.max_open_issues
        if len(open_tasks(snapshot.tasks)) > limit:
            msg = f"The board has more than {limit} open issues."
            raise BoardTooLargeError(msg, hint="Use a board with a narrower filter.")

    def _partial(self, snapshot: BoardSnapshot) -> PartialResult:
        tz = self._settings.team.time_zone
        timeout = self._settings.limits.tool_timeout_s
        flow = flow_metrics(
            snapshot,
            local_date(self._clock(), tz),
            tz,
            self.settings_for(snapshot.site_id).limits.reporting_window_days,
        )
        return PartialResult(
            site_id=snapshot.site_id,
            board_id=snapshot.board_id,
            open_issues=len(open_tasks(snapshot.tasks)),
            flow=flow,
            throughput=self.throughput([snapshot], local_date(self._clock(), tz)),
            keys={t.issue_id: t.issue_key for t in snapshot.tasks},
            reason=(
                f"timeout: the schedule was not ready within {timeout:g} s "
                "(limits.tool_timeout_s); only flow metrics are returned. Call pb_build_plan "
                "again: the board data is stored, so the next build is faster."
            ),
        )

    def throughput(self, snapshots: Sequence[BoardSnapshot], plan_date: date) -> Throughput:
        """Weekly throughput over the history window, capped by the fetch window."""
        tasks = tuple({t.issue_id: t for s in snapshots for t in s.tasks}.values())
        limits = (
            self.settings_for(snapshots[0].site_id).limits if snapshots else self._settings.limits
        )
        return weekly_throughput(
            tasks,
            plan_date,
            self._settings.team.time_zone,
            limits.throughput_history_weeks,
            limits.reporting_window_days,
        )

    def flow(
        self, snapshot: BoardSnapshot, plan_date: date, window_days: int | None
    ) -> FlowMetrics:
        """Flow metrics for one board over a window (default: the reporting window)."""
        window = self.flow_window(snapshot.site_id, window_days)
        return flow_metrics(snapshot, plan_date, self._settings.team.time_zone, window)

    def flow_window(self, site_id: SiteId, window_days: int | None) -> int:
        """The flow window in days: the given one, or the site's reporting window.

        Raises:
            InvalidArgumentError: ``window_days`` is not positive.
        """
        if window_days is None:
            return self.settings_for(site_id).limits.reporting_window_days
        if window_days < 1:
            msg = f"window_days must be at least 1 (got {window_days})."
            raise InvalidArgumentError(msg, hint="Omit it for the reporting window.")
        return window_days

    def workload(
        self, plan: Plan, account_id: str | None = None, weeks: int | None = None
    ) -> tuple[WeekLoad, ...]:
        """Person-week load of a plan, from the plan's settings (checked by ``get_plan``).

        Raises:
            InvalidArgumentError: ``weeks`` is not positive, or ``account_id`` has no work in
                the plan.
            PlanExpiredError: the plan's board data is gone.
        """
        if weeks is not None and weeks < 1:
            msg = f"weeks must be at least 1 (got {weeks})."
            raise InvalidArgumentError(msg, hint="Omit it for every week of the plan.")
        settings = self.settings_for(plan.site_id)
        calendar = calendar_from_settings(
            plan.plan_date,
            settings.team,
            settings.limits.planning_horizon_years,
            people_of(self.snapshots_for(plan)),
        )
        loads = plan_workload(plan.scheduled_tasks, calendar, settings.team.hours_per_day)
        if account_id is not None:
            lanes = {t.lane for t in plan.scheduled_tasks if t.lane is not None}
            if account_id not in lanes:
                msg = f"No work in this plan is assigned to '{account_id}'."
                hint = "Use an account ID from pb_workload without account_id."
                raise InvalidArgumentError(msg, hint=hint)
            loads = tuple(w for w in loads if w.account_id == account_id)
        if weeks is not None:
            starts = sorted({w.week_start for w in loads})[:weeks]
            loads = tuple(w for w in loads if w.week_start in starts)
        return loads

    def scoped_forecast(
        self, plan: Plan, kind: MilestoneKind, scope_id: int
    ) -> tuple[Forecast, Forecast | None]:
        """(primary, alternative) forecast for one fix version or epic of a plan.

        Raises:
            InvalidArgumentError: no open milestone ``scope_id`` of that kind in the plan.
            PlanExpiredError: the plan's board data is gone.
        """
        milestone = next(
            (m for m in plan.milestones if m.kind is kind and m.milestone_id == scope_id), None
        )
        if milestone is None or milestone.forecast_date is None:
            msg = f"No open {kind.value} {scope_id} in this plan."
            raise InvalidArgumentError(msg, hint="See pb_milestones for IDs.")
        snapshots = self.snapshots_for(plan)
        settings = self.settings_for(plan.site_id)
        linked = [
            t
            for s in snapshots
            for t in s.tasks
            if t.status_category is not StatusCategory.DONE
            and (
                scope_id in t.fix_version_ids
                if kind is MilestoneKind.FIX_VERSION
                else t.epic_issue_id == scope_id
            )
        ]
        calendar = calendar_from_settings(
            plan.plan_date,
            settings.team,
            settings.limits.planning_horizon_years,
            people_of(snapshots),
        )
        mc = monte_carlo_forecast(
            len(linked),
            self.throughput(snapshots, plan.plan_date),
            calendar,
            trials=settings.limits.monte_carlo_trials,
            seed=seed_for(plan.plan_id) + milestone.milestone_id,
            min_history_weeks=settings.limits.min_history_weeks,
        )
        schedule = schedule_forecast(
            milestone.forecast_date, plan.estimated_share, settings.team, solved=True
        )
        return choose_primary(schedule, mc, plan.estimated_share, settings.team.estimated_threshold)

    def today(self) -> date:
        """Today in the team time zone."""
        return local_date(self._clock(), self._settings.team.time_zone)

    @property
    def settings(self) -> Settings:
        """The active settings."""
        return self._settings

    async def snapshot_for_link(self, board_link: str) -> BoardSnapshot:
        """The current snapshot of a board (refreshing if needed), without building a plan."""
        ref = parse_board_link(board_link, self._registry)
        site = self._registry.get(ref.site_id)
        refreshed = await anyio.to_thread.run_sync(
            lambda: self._refresher(site.site_id).refresh(
                self._source(site), site.site_id, ref.board_id
            ),
            abandon_on_cancel=True,
        )
        return refreshed.snapshot

    def _stale_plan(self, site: SiteId, board: BoardId) -> Plan:
        account = self._store.known_account(site, board)
        plan = (
            self._store.latest_plan(site, account, (board,), self._for_site(site)[1])
            if account
            else None
        )
        max_age = timedelta(hours=self._settings.limits.stale_plan_max_hours)
        if plan is None or self._clock() - plan.generated_at > max_age:
            msg = "Jira is unreachable and there is no recent plan to fall back on."
            raise JiraUnavailableError(msg, hint="Retry later.")
        context = WarningContext(
            tasks={},
            all_tasks={},
            efforts={},
            scheduled={},
            sprints={},
            time_zone=self._settings.team.time_zone,
            stale_since=plan.generated_at,
        )
        stale_rules = [r for r in self._rules if r.rule_id is WarningId.STALE_PLAN]
        found = run_rules(stale_rules, context, self._settings.disabled_warnings)
        warnings: tuple[PlanWarning, ...] = (*found, *plan.warnings)
        return plan.model_copy(update={"stale_since": plan.generated_at, "warnings": warnings})
