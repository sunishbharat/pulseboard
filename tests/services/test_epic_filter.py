"""Gantt epic filter: ``pb_render(epics=…)`` from fixture board to rendered export (Addendum 02
§6.6). Filtering changes what is shown, never the schedule, and the result says what it left out."""

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from factories import snapshot, task
from fakes import LINK, FakeSource
from gantt_fixtures import PAYMENTS, scrum_board
from pulseboard.app import App
from pulseboard.domain.snapshot import BoardSnapshot, Epic, ExternalBlocker
from pulseboard.domain.types import (
    ExportFormat,
    IssueId,
    IssueKey,
    PlanId,
    ReportId,
    StatusCategory,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.errors import InvalidArgumentError
from pulseboard.services.exports import RenderOptions, resolve_epics

McpClient = Client[FastMCPTransport]
DONE_EPIC = IssueId(92)
PAYMENTS_OPEN = ("ABC-104", "ABC-106", "ABC-111", "ABC-109", "ABC-112")
SEARCH_ONLY = "Synonym dictionary import"
"""ABC-110's summary: a Search task with no links, so nothing of it may show under Payments."""


def board() -> BoardSnapshot:
    """The Scrum fixture plus an epic whose only task is done, and external blockers on ABC-111:
    one open (counted) and one done (not counted)."""
    base = scrum_board()
    blockers = (
        ExternalBlocker(
            issue_id=IssueId(7), issue_key=IssueKey("XYZ-7"), status_category=StatusCategory.TO_DO
        ),
        ExternalBlocker(
            issue_id=IssueId(8), issue_key=IssueKey("XYZ-8"), status_category=StatusCategory.DONE
        ),
    )
    tasks = tuple(
        t.model_copy(update={"epic_issue_id": DONE_EPIC})
        if t.issue_id == 101
        else t.model_copy(update={"external_blockers": blockers})
        if t.issue_id == 111
        else t
        for t in base.tasks
    )
    done = Epic(issue_id=DONE_EPIC, issue_key=IssueKey("ABC-92"), name=UntrustedText("Archive"))
    return base.model_copy(update={"tasks": tasks, "epics": (*base.epics, done)})


@pytest.fixture
def source() -> FakeSource:
    return FakeSource([board()])


async def build(app: App) -> PlanId:
    result = await app.service.build(LINK)
    assert result.plan is not None
    return result.plan.plan_id


def render_csv(app: App, plan_id: PlanId, *epics: str) -> tuple[tuple[str, ...], list[str]]:
    export = app.exports.render(
        plan_id, ReportId.GANTT, ExportFormat.CSV, RenderOptions(epics=epics)
    )
    lines = app.exports.read(export.record.export_id)[1].decode().splitlines()
    return export.summary, [line.split(",")[0] for line in lines[1:]]


# ---------------------------------------------------------------- resolving epic names


@pytest.mark.parametrize("name", ["ABC-90", "abc-90", " ABC-90 ", "90"])
def test_epic_keys_match_trimmed_and_case_insensitive_or_by_id(name: str) -> None:
    assert resolve_epics([name], [board()]) == (PAYMENTS,)


def test_unknown_epic_is_an_invalid_argument_naming_the_board_epics() -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        resolve_epics(["ABC-999"], [board()])

    assert caught.value.message == "No epic 'ABC-999' in this plan."
    assert caught.value.hint == "Epics on the board: ABC-90, ABC-91, ABC-92."
    with pytest.raises(InvalidArgumentError, match="ABC-90") as no_epics:
        resolve_epics(["ABC-90"], [snapshot((task(1),))])
    assert no_epics.value.hint == "The board has no epics."


@pytest.mark.parametrize(("count", "cut"), [(9, False), (10, False), (11, True)])
def test_hint_lists_at_most_ten_epics_and_says_when_it_cuts(count: int, cut: bool) -> None:
    epics = tuple(
        Epic(issue_id=IssueId(n), issue_key=IssueKey(f"E-{n:02d}"), name=UntrustedText("x"))
        for n in range(count)
    )
    with pytest.raises(InvalidArgumentError) as caught:
        resolve_epics(["nope"], [snapshot((task(1),)).model_copy(update={"epics": epics})])

    hint = caught.value.hint or ""
    assert hint.count("E-") == min(count, 10)
    assert hint.endswith(" ….") is cut


# ---------------------------------------------------------------- rendering


async def test_gantt_shows_only_the_epic_and_says_what_it_left_out(app: App) -> None:
    plan_id = await build(app)
    summary, keys = render_csv(app, plan_id, "abc-90")

    assert tuple(keys) == PAYMENTS_OPEN
    assert summary == (
        "Filtered to ABC-90 Payments: 5 of 9 open tasks; 1 blocker outside the filter "
        "(hidden stubs); 1 open blocker outside the board.",
        "Shown work ends 2026-11-13; 2 of these tasks are critical.",
        "Board forecast (schedule): P85 2026-11-13, medium.",
    )


async def test_changing_the_epics_changes_the_output(app: App) -> None:
    plan_id = await build(app)
    _, payments = render_csv(app, plan_id, "ABC-90")
    _, search = render_csv(app, plan_id, "ABC-91")
    _, both = render_csv(app, plan_id, "ABC-90", "ABC-91")
    whole_summary, whole = render_csv(app, plan_id)

    assert set(payments).isdisjoint(search)
    assert sorted(both) == sorted(whole) == sorted(payments + search)
    assert whole_summary[0] == "9 open tasks scheduled; the last ends 2026-11-13."


async def test_epic_with_no_open_work_is_explained(app: App) -> None:
    plan_id = await build(app)
    summary, keys = render_csv(app, plan_id, "ABC-92")
    page = app.exports.render(
        plan_id, ReportId.GANTT, ExportFormat.HTML, RenderOptions(epics=("ABC-92",))
    )
    html = app.exports.read(page.record.export_id)[1].decode()

    assert keys == []
    assert summary[0].startswith("Filtered to ABC-92 Archive: 0 of 9 open tasks;")
    assert summary[1] == "No open tasks match this filter."
    assert "No tasks match this filter" in html


async def test_epic_filter_on_another_report_is_refused(app: App) -> None:
    plan_id = await build(app)

    with pytest.raises(InvalidArgumentError, match="gantt report only, not 'workload'"):
        app.exports.render(
            plan_id, ReportId.WORKLOAD, ExportFormat.HTML, RenderOptions(epics=("ABC-90",))
        )


# ---------------------------------------------------------------- end to end over MCP


async def test_pb_render_epic_gantt_end_to_end(client: McpClient) -> None:
    built = await client.call_tool("pb_build_plan", {"board_link": LINK})
    plan_id = (built.structured_content or {})["plan_id"]
    rendered = await client.call_tool(
        "pb_render", {"plan_id": plan_id, "report_id": "gantt", "epics": [" abc-90"]}
    )
    out = rendered.structured_content or {}
    page = getattr((await client.read_resource(out["uri"]))[0], "text", "")

    assert out["summary"][0].startswith("Filtered to ABC-90 Payments: 5 of 9 open tasks;")
    assert "Filtered to ABC-90 Payments" in page  # the banner in the chart header
    assert "Payments (filtered)" in page  # the summary row
    assert "ABC-107 (hidden)" in page  # ABC-109's blocker from the Search epic
    assert SEARCH_ONLY not in page

    unknown = await client.call_tool(
        "pb_render",
        {"plan_id": plan_id, "report_id": "gantt", "epics": ["ABC-999"]},
        raise_on_error=False,
    )
    other = await client.call_tool(
        "pb_render",
        {"plan_id": plan_id, "report_id": "throughput", "epics": ["ABC-90"]},
        raise_on_error=False,
    )
    texts = [" ".join(getattr(c, "text", "") for c in r.content) for r in (unknown, other)]
    assert texts[0].startswith("invalid_argument: No epic 'ABC-999' in this plan.")
    assert texts[1].startswith("invalid_argument: epics filters the gantt report only")
