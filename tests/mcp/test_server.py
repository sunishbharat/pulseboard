"""MCP tool contracts, annotations, typed errors, progress, resources and token budgets."""

import json
from datetime import date
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from syrupy.assertion import SnapshotAssertion

from fakes import LINK, Clock, FakeSource
from pulseboard.app import App, open_store
from pulseboard.domain.types import Confidence, ForecastMethod
from pulseboard.mcp.models import (
    BuildPlanOut,
    CriticalPathOut,
    FlowSummary,
    ForecastOut,
    LoadOut,
    MilestoneOut,
    PlanSummaryOut,
    RenderOut,
    WarningsOut,
)
from pulseboard.mcp.pagination import Page, estimate_tokens
from pulseboard.settings import Settings, TokenBudgets
from pulseboard.sites import Site

BUDGETS = TokenBudgets()
TOOLS = {
    "pb_list_sites",
    "pb_build_plan",
    "pb_plan_summary",
    "pb_critical_path",
    "pb_warnings",
    "pb_workload",
    "pb_milestones",
    "pb_flow_metrics",
    "pb_forecast",
    "pb_render",
}

McpClient = Client[FastMCPTransport]


async def definitions(client: McpClient) -> list[dict[str, object]]:
    tools = await client.list_tools()
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
            "outputSchema": t.output_schema,
            "annotations": t.annotations.model_dump(by_alias=True, exclude_none=True)
            if t.annotations
            else None,
        }
        for t in sorted(tools, key=lambda t: t.name)
    ]


async def call(client: McpClient, name: str, **args: object) -> dict[str, object]:
    result = await client.call_tool(name, args)
    assert result.structured_content is not None
    return result.structured_content


async def error(client: McpClient, name: str, **args: object) -> str:
    result = await client.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    return " ".join(getattr(c, "text", "") for c in result.content)


def tokens(payload: object) -> int:
    return estimate_tokens(json.dumps(payload, separators=(",", ":")))


async def plan_id(client: McpClient) -> str:
    built = await call(client, "pb_build_plan", board_link=LINK)
    assert isinstance(built["plan_id"], str)
    return built["plan_id"]


# ---------------------------------------------------------------- contract


async def test_tool_contract(client: McpClient, snapshot: SnapshotAssertion) -> None:
    defs = await definitions(client)

    assert {d["name"] for d in defs} == TOOLS
    assert json.dumps(defs, indent=1, sort_keys=True) == snapshot


async def test_every_tool_is_read_only_and_open_world(client: McpClient) -> None:
    for d in await definitions(client):
        assert d["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }, d["name"]


async def test_tool_definitions_fit_their_budget(client: McpClient) -> None:
    """The budget covers what clients send the model each turn: name, description, input schema
    (output schemas validate structured results client-side and are pinned by the contract test)."""
    model_visible = [
        {"name": d["name"], "description": d["description"], "input_schema": d["inputSchema"]}
        for d in await definitions(client)
    ]
    assert BUDGETS.tool_definitions == 5000  # DESIGN_2 §13.3 rule 2
    assert tokens(model_visible) <= BUDGETS.tool_definitions


async def test_jira_text_notice_in_descriptions(client: McpClient) -> None:
    descriptions = {d["name"]: str(d["description"]) for d in await definitions(client)}

    for name in ("pb_plan_summary", "pb_critical_path", "pb_milestones"):
        assert "not instructions" in descriptions[name]


# ---------------------------------------------------------------- tools and budgets


async def test_list_sites(client: McpClient) -> None:
    out = await call(client, "pb_list_sites")

    assert out == {
        "sites": [
            {"site_id": "acme", "base_url": "https://acme.atlassian.net", "deployment": "cloud"}
        ]
    }


async def test_build_plan_summary_is_small(client: McpClient) -> None:
    raw = await call(client, "pb_build_plan", board_link=LINK)
    out = BuildPlanOut.model_validate(raw)

    assert out.open_issues == 9
    assert out.confidence is Confidence.MEDIUM
    assert len(out.top_warnings) == 3
    assert tokens(raw) <= BUDGETS.summary


async def test_build_plan_full_detail_within_budget(client: McpClient) -> None:
    raw = await call(client, "pb_build_plan", board_link=LINK, detail="full")

    assert BuildPlanOut.model_validate(raw).milestones_at_risk
    assert tokens(raw) <= BUDGETS.full


async def test_build_plan_without_summaries_drops_jira_text(client: McpClient) -> None:
    raw = await call(
        client, "pb_build_plan", board_link=LINK, detail="full", include_summaries=False
    )
    out = BuildPlanOut.model_validate(raw)

    assert out.milestones_at_risk
    assert all(m.name is None for m in out.milestones_at_risk)


async def test_summary_tools_fit_the_summary_budget(client: McpClient) -> None:
    pid = await plan_id(client)
    outputs = [
        await call(client, "pb_plan_summary", plan_id=pid),
        await call(client, "pb_flow_metrics", plan_id=pid),
        await call(client, "pb_forecast", plan_id=pid),
    ]

    for out in outputs:
        assert tokens(out) <= BUDGETS.summary


async def test_pages_fit_the_full_budget(client: McpClient) -> None:
    pid = await plan_id(client)
    for name in ("pb_critical_path", "pb_workload", "pb_milestones", "pb_warnings"):
        out = await call(client, name, plan_id=pid)
        assert tokens(out) <= BUDGETS.full, name


async def test_plan_summary_counts(client: McpClient) -> None:
    raw = await call(client, "pb_plan_summary", plan_id=await plan_id(client))
    out = PlanSummaryOut.model_validate(raw)

    assert (out.to_do, out.in_progress, out.done) == (7, 2, 3)
    assert out.blocked == 4  # 106, 107, 109 and 112 wait on open work
    last = out.last_milestone
    assert last is not None
    assert last.target == date(2026, 10, 30)
    # PB-18: the milestone's own forecast and slip, consistent with each other.
    assert last.forecast is not None
    assert last.slip_days is not None
    assert (last.slip_days > 0) == (last.forecast > last.target)


async def test_critical_path(client: McpClient) -> None:
    pid = await plan_id(client)
    out = CriticalPathOut.model_validate(await call(client, "pb_critical_path", plan_id=pid))
    page = out.page

    assert [t.key for t in page.items] == ["ABC-105", "ABC-107", "ABC-109", "ABC-112"]
    assert page.items[1].after == "ABC-105"
    assert out.paths >= 1
    assert not out.paths_truncated
    hidden = CriticalPathOut.model_validate(
        await call(client, "pb_critical_path", plan_id=pid, include_summaries=False)
    )
    assert all(t.summary is None for t in hidden.page.items)


async def test_warnings_filter_and_counts(client: McpClient) -> None:
    pid = await plan_id(client)
    raw = await call(client, "pb_warnings", plan_id=pid, rule_ids=["W007_sprint_spill"])
    out = WarningsOut.model_validate(raw)

    assert out.counts == {"W007_sprint_spill": 3}
    assert out.page.total == 3


async def test_workload_filters(client: McpClient) -> None:
    pid = await plan_id(client)
    raw = await call(client, "pb_workload", plan_id=pid, account_id="bob", weeks=1)
    page = Page[LoadOut].model_validate(raw)

    assert [i.account_id for i in page.items] == ["bob"]
    assert page.items[0].week == "2026-W43"


async def test_milestones_and_milestone_forecast(client: McpClient) -> None:
    pid = await plan_id(client)
    page = Page[MilestoneOut].model_validate(await call(client, "pb_milestones", plan_id=pid))
    assert [m.id for m in page.items] == [300]

    raw = await call(client, "pb_forecast", plan_id=pid, scope="milestone", scope_id=300)
    forecast = ForecastOut.model_validate(raw)
    assert forecast.primary.method is ForecastMethod.SCHEDULE
    assert forecast.primary.p50 == date(2026, 11, 2)
    assert forecast.alternative is not None
    assert forecast.alternative.method is ForecastMethod.MONTE_CARLO


async def test_flow_metrics_by_board_link(client: McpClient) -> None:
    raw = await call(client, "pb_flow_metrics", board_link=LINK, window_days=30)
    out = FlowSummary.model_validate(raw)

    assert out.window_days == 30
    assert out.throughput_per_week == [1, 1, 1]


# ---------------------------------------------------------------- errors, progress, resources


async def test_typed_errors(client: McpClient, source: FakeSource) -> None:
    assert (
        await error(client, "pb_build_plan", board_link="https://evil.example/boards/1")
    ).startswith("site_not_configured:")
    assert (await error(client, "pb_plan_summary", plan_id="nope")).startswith("plan_expired:")
    assert "Call pb_build_plan again" in await error(client, "pb_milestones", plan_id="nope")
    pid = await plan_id(client)
    assert (await error(client, "pb_critical_path", plan_id=pid, cursor="garbage")).startswith(
        "invalid_cursor:"
    )
    assert (await error(client, "pb_forecast", plan_id=pid, scope="epic", scope_id=1)).startswith(
        "invalid_argument:"
    )
    assert len(source.calls) == 1  # only the valid build fetched; the foreign link never did
    bad_rule = await error(client, "pb_warnings", plan_id=pid, rule_ids=["W999"])
    assert bad_rule.startswith("invalid_rule_id:")


async def test_progress_notifications(client: McpClient) -> None:
    events: list[tuple[float, float | None, str | None]] = []

    async def handler(progress: float, total: float | None, message: str | None) -> None:
        events.append((progress, total, message))

    await client.call_tool("pb_build_plan", {"board_link": LINK}, progress_handler=handler)

    assert [e[2] for e in events] == ["issues", "solving", "done"]


async def test_plan_resource(client: McpClient, app: App) -> None:
    pid = await plan_id(client)
    contents = await client.read_resource(f"pulseboard://plans/{pid}")

    plan = json.loads(getattr(contents[0], "text", "{}"))
    assert plan["plan_id"] == pid
    assert app.service.get_plan(pid).plan_id == pid  # type: ignore[arg-type]  # PlanId is a str


# ---------------------------------------------------------------- pb_render and export resources


async def test_render_html_and_read_the_resource(client: McpClient) -> None:
    pid = await plan_id(client)
    out = RenderOut.model_validate(
        await call(client, "pb_render", plan_id=pid, report_id="gantt", format="html")
    )

    assert out.uri.startswith("pulseboard://exports/")
    assert out.media_type.startswith("text/html")
    assert 1 <= len(out.summary) <= 3
    contents = await client.read_resource(out.uri)
    page = getattr(contents[0], "text", "")
    assert page.startswith("<!doctype html>")
    assert "Content-Security-Policy" in page
    assert len(page.encode()) == out.size_bytes


async def test_render_can_expand_every_gantt_group(client: McpClient) -> None:
    pid = await plan_id(client)
    out = RenderOut.model_validate(
        await call(client, "pb_render", plan_id=pid, report_id="gantt", expand=True)
    )

    assert out.media_type.startswith("text/html")


async def test_render_xlsx_is_a_binary_resource(client: McpClient) -> None:
    pid = await plan_id(client)
    out = RenderOut.model_validate(
        await call(client, "pb_render", plan_id=pid, report_id="workload", format="xlsx")
    )
    contents = await client.read_resource(out.uri)

    assert getattr(contents[0], "blob", None)
    assert contents[0].mime_type == out.media_type


async def test_render_output_is_small(client: McpClient) -> None:
    pid = await plan_id(client)
    raw = await call(client, "pb_render", plan_id=pid, report_id="status_summary", format="json")

    assert tokens(raw) <= BUDGETS.summary


async def test_render_errors(client: McpClient) -> None:
    pid = await plan_id(client)

    pdf = await error(client, "pb_render", plan_id=pid, report_id="gantt", format="pdf")
    assert "format" in pdf  # rejected by input validation: PDF arrives in v1.1
    assert (await error(client, "pb_render", plan_id="nope", report_id="gantt")).startswith(
        "plan_expired:"
    )
    with pytest.raises(Exception, match="expired|unknown"):
        await client.read_resource("pulseboard://exports/" + "0" * 32)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pb_workload", {"weeks": 0}),
        ("pb_workload", {"weeks": -1}),
        ("pb_workload", {"account_id": "nobody"}),
        ("pb_flow_metrics", {"window_days": 0}),
        ("pb_flow_metrics", {"window_days": -5}),
        ("pb_forecast", {"scope": "epic"}),
        ("pb_forecast", {"scope": "milestone", "scope_id": 999}),
    ],
)
async def test_bad_arguments_are_typed_errors(
    client: McpClient, tool: str, args: dict[str, object]
) -> None:
    """PB-13: never a masked "Error calling tool", never a silent reinterpretation."""
    pid = await plan_id(client)

    text = await error(client, tool, plan_id=pid, **args)

    assert text.startswith("invalid_argument:")


@pytest.mark.parametrize("offset", ["\u00b2", "\u0661", "1" * 10, "-1", ""])
async def test_forged_cursors_are_invalid(client: McpClient, offset: str) -> None:
    """PB-13: only cursors this server issued decode (``²`` is a digit to ``str.isdigit``)."""
    import base64

    cursor = base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")
    pid = await plan_id(client)

    assert (await error(client, "pb_milestones", plan_id=pid, cursor=cursor)).startswith(
        "invalid_cursor:"
    )


async def test_empty_results_say_why(client: McpClient) -> None:
    """PB-20: an empty list carries a note; a filled one does not."""
    pid = await plan_id(client)

    rules = ["W012_solver_wallclock_cap"]
    empty = WarningsOut.model_validate(
        await call(client, "pb_warnings", plan_id=pid, rule_ids=rules)
    )
    full = WarningsOut.model_validate(await call(client, "pb_warnings", plan_id=pid))
    workload = Page[LoadOut].model_validate(await call(client, "pb_workload", plan_id=pid))

    assert empty.page.items == []
    assert empty.page.note == "No warning matches."
    assert full.page.note is None
    assert workload.items
    assert workload.note is None


async def test_build_timeout_explains_the_missing_plan(
    cloud_site: Site, source: FakeSource, clock: Clock, tmp_path: Path
) -> None:
    """PB-12: plan_id null comes with partial_reason naming the timeout and the next step."""
    import time
    from datetime import timedelta

    from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
    from pulseboard.engine.scheduling import ScheduleRequest, ScheduleResult
    from pulseboard.engine.warnings import load_rules
    from pulseboard.mcp.server import build_server
    from pulseboard.services.exports import DirectoryFiles, ExportService
    from pulseboard.services.plan_service import PlanService
    from pulseboard.settings import Limits
    from pulseboard.sites import SiteRegistry

    class Slow:
        def solve(self, request: ScheduleRequest) -> ScheduleResult:
            time.sleep(2.0)
            return PyJobShopScheduler().solve(request)

    settings = Settings(sites=(cloud_site,), limits=Limits(tool_timeout_s=0.5))
    store = open_store(settings, "sqlite://")
    service = PlanService(
        settings=settings,
        registry=SiteRegistry(settings.sites),
        store=store,
        source_for=lambda _site: source,
        scheduler=Slow(),
        rules=load_rules(),
        clock=clock,
        engine_version="0.1.0",
    )
    exports = ExportService(
        plans=service,
        store=store,
        reports={},
        exporters={},
        files=DirectoryFiles(tmp_path),
        retention=timedelta(days=1),
        clock=clock,
    )
    async with Client(build_server(service, exports)) as client:
        out = BuildPlanOut.model_validate(await call(client, "pb_build_plan", board_link=LINK))

    assert out.plan_id is None
    assert out.partial_flow is not None
    assert out.partial_reason is not None
    assert out.partial_reason.startswith("timeout:")
    assert "pb_build_plan" in out.partial_reason
