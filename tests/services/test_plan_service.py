"""PlanService (DESIGN §9.6, §11.1, §11.3, §11.4, §12.4, §2.3; §18.1 #19)."""

import time
from datetime import timedelta

import anyio
import pytest

from factories import fixture_board, snapshot, task
from fakes import Clock, FakeSource
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    PlanId,
    RequestProfile,
    SiteId,
    SolverStatus,
    WarningId,
)
from pulseboard.engine.forecaster import seed_for
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.scheduling import Scheduler, ScheduleRequest, ScheduleResult
from pulseboard.engine.warnings import load_rules
from pulseboard.errors import (
    BoardTooLargeError,
    InvalidArgumentError,
    JiraUnavailableError,
    PlanExpiredError,
    SiteNotConfiguredError,
    ToolTimeoutError,
)
from pulseboard.services.plan_service import (
    PlanService,
    compute_plan_id,
    settings_hash,
)
from pulseboard.settings import Limits, Settings, TeamSettings
from pulseboard.sites import Site, SiteRegistry
from pulseboard.store.repository import SnapshotStore, create_store_engine, migrate

LINK = "https://acme.atlassian.net/jira/software/projects/ABC/boards/42"


class SlowScheduler:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    def solve(self, request: ScheduleRequest) -> ScheduleResult:
        time.sleep(self.delay_s)
        return PyJobShopScheduler().solve(request)


def service(
    cloud_site: Site,
    source: FakeSource,
    clock: Clock,
    *,
    limits: Limits | None = None,
    scheduler: Scheduler | None = None,
    store: SnapshotStore | None = None,
) -> PlanService:
    if store is None:
        engine = create_store_engine("sqlite://")
        migrate(engine)
        store = SnapshotStore(engine)
    settings = Settings(sites=(cloud_site,), limits=limits or Limits())
    return PlanService(
        settings=settings,
        registry=SiteRegistry(settings.sites),
        store=store,
        source_for=lambda _site: source,
        scheduler=scheduler or PyJobShopScheduler(),
        rules=load_rules(),
        clock=clock,
        engine_version="0.1.0",
    )


async def test_build_store_and_reuse(cloud_site: Site) -> None:
    source, clock = FakeSource([fixture_board()]), Clock()
    svc = service(cloud_site, source, clock)

    first = await svc.build(LINK)
    second = await svc.build(LINK)

    assert first.plan is not None
    assert not first.cached
    assert second.cached
    assert second.plan == first.plan
    assert svc.get_plan(first.plan.plan_id) == first.plan
    assert len(source.calls) == 1


async def test_next_plan_date_gives_new_plan_id(cloud_site: Site) -> None:
    """§18.1 #19."""
    source, clock = FakeSource([fixture_board()]), Clock()
    svc = service(cloud_site, source, clock)

    today = (await svc.build(LINK)).plan
    clock.now += timedelta(days=1)
    tomorrow = (await svc.build(LINK)).plan

    assert today is not None
    assert tomorrow is not None
    assert tomorrow.plan_id != today.plan_id
    assert tomorrow.plan_date == today.plan_date + timedelta(days=1)


async def test_unknown_plan_is_expired(cloud_site: Site) -> None:
    svc = service(cloud_site, FakeSource([snapshot()]), Clock())

    with pytest.raises(PlanExpiredError) as info:
        svc.get_plan(PlanId("nope"))
    assert info.value.hint == "Call pb_build_plan again."


async def test_foreign_link_is_rejected_before_any_fetch(cloud_site: Site) -> None:
    source = FakeSource([snapshot()])
    svc = service(cloud_site, source, Clock())

    with pytest.raises(SiteNotConfiguredError):
        await svc.build("https://evil.example/jira/software/c/boards/42")
    assert source.calls == []
    assert source.account_calls == 0


async def test_stale_plan_is_served_for_up_to_24_hours(cloud_site: Site) -> None:
    source, clock = FakeSource([fixture_board()]), Clock()
    svc = service(cloud_site, source, clock)
    fresh = (await svc.build(LINK)).plan
    assert fresh is not None

    source.unavailable = True
    clock.now += timedelta(hours=23)
    stale = (await svc.build(LINK)).plan

    assert stale is not None
    assert stale.stale_since == fresh.generated_at
    assert stale.warnings[0].rule_id is WarningId.STALE_PLAN
    assert stale.plan_id == fresh.plan_id

    clock.now += timedelta(hours=2)
    with pytest.raises(JiraUnavailableError):
        await svc.build(LINK)


async def test_no_plan_to_fall_back_on(cloud_site: Site) -> None:
    source = FakeSource([snapshot()], unavailable=True)

    with pytest.raises(JiraUnavailableError):
        await service(cloud_site, source, Clock()).build(LINK)


async def test_board_too_large(cloud_site: Site) -> None:
    source = FakeSource([snapshot((task(1), task(2), task(3)))])
    svc = service(cloud_site, source, Clock(), limits=Limits(max_open_issues=2))

    with pytest.raises(BoardTooLargeError):
        await svc.build(LINK)


async def test_work_beyond_planning_horizon_is_refused(cloud_site: Site) -> None:
    """No velocity history: 1 point per day, so 1,000 points cannot finish by 2027-12-31."""
    source = FakeSource([snapshot((task(1, story_points=1000.0),))])
    svc = service(cloud_site, source, Clock(), limits=Limits(planning_horizon_years=1))

    with pytest.raises(BoardTooLargeError) as info:
        await svc.build(LINK)

    assert "planning horizon (until 2027-12-31)" in info.value.message
    assert info.value.hint is not None
    assert "limits.planning_horizon_years" in info.value.hint


class UnsolvedScheduler:
    def solve(self, request: ScheduleRequest) -> ScheduleResult:
        return ScheduleResult(status=SolverStatus.NOT_SOLVED, timings=())


async def test_no_schedule_within_solver_budget_is_refused(cloud_site: Site) -> None:
    source = FakeSource([fixture_board()])
    svc = service(cloud_site, source, Clock(), scheduler=UnsolvedScheduler())

    with pytest.raises(ToolTimeoutError) as info:
        await svc.build(LINK)

    assert info.value.message == "The scheduler found no plan within its budget."


async def test_timeout_after_snapshot_returns_flow_metrics(cloud_site: Site) -> None:
    source, clock = FakeSource([fixture_board()]), Clock()
    svc = service(
        cloud_site, source, clock, limits=Limits(tool_timeout_s=0.5), scheduler=SlowScheduler(3.0)
    )

    result = await svc.build(LINK)

    assert result.plan is None
    assert result.partial is not None
    assert result.partial.open_issues == 9
    assert result.partial.flow.cfd
    assert result.partial.reason.startswith("timeout:")  # PB-12: says why, and what next
    assert "limits.tool_timeout_s" in result.partial.reason


async def test_timeout_before_snapshot_raises(cloud_site: Site) -> None:
    source = FakeSource([fixture_board()], delay_s=2.0)
    svc = service(cloud_site, source, Clock(), limits=Limits(tool_timeout_s=0.3))

    with pytest.raises(ToolTimeoutError):
        await svc.build(LINK)


async def test_progress_is_reported(cloud_site: Site) -> None:
    events: list[tuple[float, float | None, str]] = []

    async def progress(done: float, total: float | None, message: str) -> None:
        events.append((done, total, message))

    await service(cloud_site, FakeSource([fixture_board()]), Clock()).build(LINK, progress)

    assert [e[2] for e in events] == ["issues", "solving", "done"]
    assert events[-1][:2] == (1.0, 1.0)


async def test_cancellation_discards_partial_work(cloud_site: Site) -> None:
    engine = create_store_engine("sqlite://")
    migrate(engine)
    store = SnapshotStore(engine)
    svc = service(
        cloud_site,
        FakeSource([fixture_board()]),
        Clock(),
        scheduler=SlowScheduler(1.0),
        store=store,
    )

    async with anyio.create_task_group() as group:
        group.start_soon(svc.build, LINK)
        await anyio.sleep(0.3)
        group.cancel_scope.cancel()

    await anyio.sleep(1.2)  # let the abandoned solver thread finish
    digest = settings_hash(Settings(sites=(cloud_site,)))
    assert store.latest_plan(SiteId("acme"), AccountId("acc-1"), (BoardId(42),), digest) is None


def plan_id(**changes: str) -> PlanId:
    values = {
        "site_id": "acme",
        "account_id": "acc-1",
        "board_ids": "42",
        "snapshot_hash": "h",
        "settings_digest": "s",
        "plan_date": "2026-10-19",
        "time_zone": "UTC",
        "engine_version": "0.1.0",
    } | changes
    return compute_plan_id(
        site_id=SiteId(values["site_id"]),
        account_id=AccountId(values["account_id"]),
        board_ids=tuple(BoardId(int(b)) for b in values["board_ids"].split(",")),
        snapshot_hash=values["snapshot_hash"],
        settings_digest=values["settings_digest"],
        plan_date=values["plan_date"],
        time_zone=values["time_zone"],
        engine_version=values["engine_version"],
    )


def test_plan_id_changes_with_every_input() -> None:
    reference = plan_id()
    changes = {
        "site_id": "corp",
        "account_id": "acc-2",
        "board_ids": "43",
        "snapshot_hash": "h2",
        "settings_digest": "s2",
        "plan_date": "2026-10-20",
        "time_zone": "Europe/Berlin",
        "engine_version": "0.2.0",
    }
    for field, other in changes.items():
        assert plan_id(**{field: other}) != reference, field
    assert plan_id(board_ids="43,42") == plan_id(board_ids="42,43")
    assert len(reference) == 32
    assert seed_for(reference) == int(reference[:8], 16)


def test_settings_hash_tracks_plan_relevant_settings(cloud_site: Site) -> None:
    base = Settings()

    assert settings_hash(base) == settings_hash(Settings(sites=(cloud_site,)))
    assert settings_hash(base) != settings_hash(Settings(team=TeamSettings(hours_per_day=6)))


async def test_polite_site_plans_with_polite_limits(cloud_site: Site) -> None:
    polite = Site(
        site_id=cloud_site.site_id,
        base_url=cloud_site.base_url,
        deployment=cloud_site.deployment,
        auth_method=cloud_site.auth_method,
        profile=RequestProfile.POLITE,
        contact="ops@example.org",
    )
    source, clock = FakeSource([fixture_board()]), Clock()
    svc = service(polite, source, clock)

    result = await svc.build(LINK)

    assert result.plan is not None
    site_settings = svc.settings_for(polite.site_id)
    assert site_settings.limits.reporting_window_days == 30
    assert result.plan.settings_hash == settings_hash(site_settings)
    assert result.plan.settings_hash != settings_hash(svc.settings)
    assert source.calls[0].window_start == svc.today() - timedelta(days=30)
    assert svc.settings_for(SiteId("removed-site")) is svc.settings


# ---------------------------------------------------------------- a plan_id is self-contained


async def test_a_plan_whose_snapshot_is_gone_is_expired(cloud_site: Site) -> None:
    """PB-04: never answer from an empty snapshot list (counts of zero)."""
    engine = create_store_engine("sqlite://")
    migrate(engine)
    store = SnapshotStore(engine)
    svc = service(cloud_site, FakeSource([fixture_board()]), Clock(), store=store)
    plan = (await svc.build(LINK)).plan
    assert plan is not None
    assert len(svc.snapshots_for(plan)) == 1
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM snapshots")

    with pytest.raises(PlanExpiredError, match="board data"):
        svc.snapshots_for(plan)


async def test_changed_settings_expire_existing_plans(cloud_site: Site) -> None:
    """PB-09: the same plan_id never answers with other settings than it was built with."""
    engine = create_store_engine("sqlite://")
    migrate(engine)
    store = SnapshotStore(engine)
    source, clock = FakeSource([fixture_board()]), Clock()
    plan = (await service(cloud_site, source, clock, store=store).build(LINK)).plan
    assert plan is not None
    same = service(cloud_site, source, clock, store=store)
    settings = Settings(sites=(cloud_site,), team=TeamSettings(hours_per_day=4.0))
    changed = PlanService(
        settings=settings,
        registry=SiteRegistry(settings.sites),
        store=store,
        source_for=lambda _site: source,
        scheduler=PyJobShopScheduler(),
        rules=load_rules(),
        clock=clock,
        engine_version="0.1.0",
    )

    assert same.get_plan(plan.plan_id) == plan
    assert same.workload(plan) == same.workload(plan)
    with pytest.raises(PlanExpiredError, match="settings changed"):
        changed.get_plan(plan.plan_id)


@pytest.mark.parametrize(
    ("weeks", "account", "error"),
    [
        (0, None, "weeks must be at least 1"),
        (-1, None, "weeks must be at least 1"),
        (None, "nobody", "No work in this plan is assigned to 'nobody'"),
    ],
)
async def test_workload_arguments_are_checked(
    cloud_site: Site, weeks: int | None, account: str | None, error: str
) -> None:
    """PB-13, PB-20: bad arguments are typed errors, never silently reinterpreted."""
    svc = service(cloud_site, FakeSource([fixture_board()]), Clock())
    plan = (await svc.build(LINK)).plan
    assert plan is not None

    with pytest.raises(InvalidArgumentError, match=error):
        svc.workload(plan, account, weeks)
    assert len({w.week_start for w in svc.workload(plan, weeks=1)}) == 1


@pytest.mark.parametrize(("window", "error"), [(0, True), (-5, True), (1, False), (None, False)])
async def test_flow_window_is_checked(cloud_site: Site, window: int | None, error: bool) -> None:
    svc = service(cloud_site, FakeSource([fixture_board()]), Clock())
    if error:
        with pytest.raises(InvalidArgumentError, match="window_days must be at least 1"):
            svc.flow_window(SiteId("acme"), window)
    else:
        assert svc.flow_window(SiteId("acme"), window) == (window or 90)
