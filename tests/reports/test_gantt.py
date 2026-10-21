"""The split-view Gantt against Addendum 02 §10 (the criteria a test can check; the review
screenshots come from ``scripts/render_gantt_review.py``)."""

import re
from collections.abc import Iterator
from typing import Any

import plotly.graph_objects as go
import pytest

from factories import snapshot, task
from gantt_fixtures import FILTERED_VIEW, kanban_board, large_board, scrum_board
from planning import SETTINGS, plan_of
from pulseboard.domain.plan import Plan
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, ReportId
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.reports.base import (
    ChartOptions,
    ColorBy,
    GroupBy,
    ReportContext,
    build_context,
    load_reports,
)
from pulseboard.reports.gantt import TABLE_COLUMNS, RowKind, build_rows, link_path, tiers_for
from pulseboard.reports.theme import PALETTES, ThemeMode

DAY = 86_400_000
INTERNAL_VALUES = (
    "story_points",
    "estimate_hours",
    "plan_date",
    "release_date",
    "to_do",
    "in_progress",
    "monte_carlo",
    "constraint",
)


@pytest.fixture(scope="module")
def board() -> BoardSnapshot:
    return scrum_board()


@pytest.fixture(scope="module")
def plan(board: BoardSnapshot) -> Plan:
    return plan_of(board)


def context(board: BoardSnapshot, plan: Plan, **options: object) -> ReportContext:
    return build_context(plan, [board], SETTINGS, ChartOptions(**options))  # type: ignore[arg-type]


def gantt(ctx: ReportContext) -> go.Figure:
    figure = load_reports()[ReportId.GANTT].build(ctx).figure
    assert figure is not None
    return figure


def trace(figure: go.Figure, name: str) -> Any:  # noqa: ANN401 - plotly traces are untyped
    return next(t for t in figure.data if t.name == name)


def hovers(figure: go.Figure) -> Iterator[str]:
    for t in figure.data:
        yield from t.hovertext or ()


def legend(figure: go.Figure) -> set[str]:
    traces = {t.name for t in figure.data if t.showlegend is not False}
    shapes = {s.name for s in figure.layout.shapes if s.showlegend}
    return traces | shapes


# ---------------------------------------------------------------- layout (§4, §6.1)


def test_split_view_rows_share_one_axis(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    layout = figure.layout
    rows = len(trace(figure, "ID").y)

    assert layout.xaxis.domain == (0, 0.45)
    assert layout.xaxis2.domain == (0.45, 1)
    assert layout.yaxis2.matches == "y"  # table and timeline rows align exactly
    assert layout.yaxis.domain == layout.yaxis2.domain
    assert layout.yaxis.range == (rows - 0.5, -0.5)
    for header, _ in TABLE_COLUMNS:
        column = trace(figure, header)
        assert (column.xaxis, column.yaxis) == ("x", "y")
        assert list(column.y) == list(range(rows))
    bars = trace(figure, "Task")
    assert (bars.xaxis, bars.yaxis) == ("x2", "y2")


def test_table_columns(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    headers = [a.text for a in figure.layout.annotations if a.xref == "x"]
    names = list(trace(figure, "Task name").text)
    row = next(i for i, n in enumerate(names) if "ABC-109" in n)

    assert headers == [f"<b>{h}</b>" for h, _ in TABLE_COLUMNS]
    assert "<b>▾ ABC-90 Payments</b>" in names[0]
    assert "ABC-109</a> Payment API retri" in names[row]
    assert trace(figure, "Dur.").text[row] == "5d"
    assert (trace(figure, "Start").text[row], trace(figure, "Finish").text[row]) == (
        "Oct 27",
        "Nov 2",
    )
    assert trace(figure, "Pred.").text[row] == "3,10"  # rows of ABC-106 and ABC-107
    assert trace(figure, "Resource").text[row] == "Alice"
    assert "Unassigned" in " ".join(trace(figure, "Resource").text)


# ---------------------------------------------------------------- grouping (§6.4)


def test_grouping_by_epic_is_the_default(board: BoardSnapshot, plan: Plan) -> None:
    rows, collapsed = build_rows(context(board, plan), plan.scheduled_tasks)
    kinds = [r.kind for r in rows]

    assert not collapsed
    assert [r.name for r in rows if r.kind is RowKind.SUMMARY] == [
        "ABC-90 Payments",
        "ABC-91 Search",
    ]
    assert kinds.count(RowKind.TASK) == 9
    assert kinds.count(RowKind.MILESTONE) == 1
    assert [r.number for r in rows] == list(range(1, 13))
    payments = rows[0]
    assert (payments.start, payments.end) == (rows[1].start, max(r.end for r in rows[1:7]))
    assert payments.critical


def test_grouping_by_assignee_and_none(board: BoardSnapshot, plan: Plan) -> None:
    by_person, _ = build_rows(context(board, plan, group_by=GroupBy.ASSIGNEE), plan.scheduled_tasks)
    flat, _ = build_rows(context(board, plan, group_by=GroupBy.NONE), plan.scheduled_tasks)
    labels = trace(gantt(context(board, plan, group_by=GroupBy.ASSIGNEE)), "Resource labels").text

    assert {r.name for r in by_person if r.kind is RowKind.SUMMARY} == {
        "Alice",
        "Bob",
        "Carol",
        "Unassigned",
    }
    assert all(t.strip(" ").startswith("ABC-") for t in labels)  # the key, not the group
    assert not [r for r in flat if r.kind is RowKind.SUMMARY]
    assert len(flat) == 10
    assert [r.start for r in flat] == sorted(r.start for r in flat)


# ---------------------------------------------------------------- encoding (§5, §6.3)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_task_bars_blue_and_critical_bars_red(
    board: BoardSnapshot, plan: Plan, mode: ThemeMode
) -> None:
    figure = gantt(context(board, plan, theme=mode))
    palette = PALETTES[mode]
    critical = sum(1 for t in plan.scheduled_tasks if t.critical)

    assert trace(figure, "Task").marker.color == palette.task
    assert trace(figure, "Critical task").marker.color == palette.critical
    assert len(trace(figure, "Critical task").y) == critical
    assert len(trace(figure, "Task").y) == 9 - critical


def test_critical_is_visible_without_color(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    printed = gantt(context(board, plan, patterns=True))
    critical = trace(figure, "Critical task")

    assert len(trace(figure, "Critical marker").y) >= len(critical.y)  # table marker
    assert all("<b>Critical</b>" in h for h in critical.hovertext)
    assert not any("Critical" in h for h in trace(figure, "Task").hovertext)
    assert trace(printed, "Critical task").marker.pattern.shape == "/"  # print pattern
    assert trace(figure, "Critical task").marker.pattern.shape is None


def test_progress_resource_labels_and_milestones(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    progress = trace(figure, "Progress")
    milestone = trace(figure, "Milestone")

    assert DAY // 2 in list(progress.x)  # ABC-104: in progress, one day long → half a day
    assert "Nov 2  ⚑ +1d vs Oct 30" in milestone.text[0]
    assert milestone.marker.symbol == "diamond"
    labels = [t.strip(" ") for t in trace(figure, "Resource labels").text]
    assert sorted(set(labels)) == ["Alice", "Bob", "Carol", "Unassigned"]
    assert not [t.name for t in figure.data if t.type == "bar" and t.text]  # no text in bars


# ---------------------------------------------------------------- link lines (§6.5)


def _segments(path: str) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    points = [tuple(map(float, p.split(","))) for p in re.findall(r"[ML]([-\d.]+,[-\d.]+)", path)]
    return list(zip(points, points[1:], strict=False))  # type: ignore[arg-type]


def test_link_lines_are_right_angled_and_toggle_together(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    links = [s for s in figure.layout.shapes if s.legendgroup == "links"]
    paths = [s for s in links if s.type == "path" and s.xsizemode != "pixel"]

    assert len(paths) == len(plan.dependency_edges) + 1  # and the milestone's predecessor
    for shape in paths:
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in _segments(shape.path))
    assert [s.name for s in links if s.showlegend] == ["Dependency"]
    assert all(s.layer == "between" for s in links)  # above nonworking bands, below bars
    red = PALETTES[ThemeMode.LIGHT].critical
    assert any(s.line.color == red and s.line.width == 1.5 for s in paths)


def test_link_routing() -> None:
    gap = link_path(0, 1, 3 * DAY, 4, DAY // 2)
    touching = link_path(0, 1, 0, 4, DAY // 2)

    assert gap == [(0, 1.0), (0, 4.0), (3 * DAY, 4.0)]
    assert touching[0] == (0, 1.0)
    assert touching[-1] == (0, 4.0)
    assert touching[2][1] == 1.5  # drops below the predecessor, then enters from the left
    assert link_path(0, 4, 0, 1, DAY // 2)[2][1] == 3.5  # a successor above goes up instead


# ---------------------------------------------------------------- timescale (§6.2)


def test_timescale_adapts_to_the_visible_range() -> None:
    from datetime import date

    short = tiers_for(date(2026, 10, 19), date(2026, 11, 2))
    medium = tiers_for(date(2026, 10, 19), date(2026, 12, 19))
    long = tiers_for(date(2026, 10, 1), date(2027, 4, 1))

    assert short.top_labels[0][1] == "Oct 19 · Wk 43"
    assert [t for _, t in short.bottom_labels[:7]] == list("MTWTFSS")
    assert [t for _, t in medium.top_labels] == ["Oct 2026", "Nov 2026", "Dec 2026"]
    assert [t for _, t in medium.bottom_labels[:3]] == ["19", "26", "2"]
    assert [t for _, t in long.top_labels] == ["Q4 2026", "Q1 2027"]
    assert [t for _, t in long.bottom_labels[:2]] == ["Oct", "Nov"]
    assert "2026-11-01" in medium.top_bounds


def test_nonworking_time_today_and_range_buttons(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan))
    shapes = figure.layout.shapes
    bands = [s for s in shapes if s.name == "Nonworking time"]
    buttons = figure.layout.updatemenus[0].buttons

    assert ("2026-10-24", "2026-10-26") in {(b.x0, b.x1) for b in bands}  # a weekend
    assert sum(1 for b in bands if b.showlegend) == 1
    assert any("Veterans Day" in h for h in trace(figure, "Holidays").hovertext)
    today = next(s for s in shapes if s.name == "Today")
    assert (today.x0, today.line.dash) == ("2026-10-19", "dash")
    assert [b.label for b in buttons] == ["2 weeks", "Sprint", "Month", "Quarter", "All"]
    two_weeks = buttons[0].args[0]
    assert two_weeks["xaxis2.range"] == ["2026-10-19", "2026-11-02"]
    assert two_weeks["xaxis2.ticktext"][:7] == list("MTWTFSS")


# ---------------------------------------------------------------- header, legend, hover (§6.7–6.9)


def test_header_shows_key_facts(board: BoardSnapshot, plan: Plan) -> None:
    texts = [a.text for a in gantt(context(board, plan)).layout.annotations]

    assert texts[0] == "<b>Team board – Delivery plan</b>"
    assert "plan date Oct 19, 2026" in texts[1]
    assert "Critical tasks: <b>4</b>" in texts[2]
    assert f"Warnings: <b>{len(plan.warnings)}</b>" in texts[2]


def test_low_confidence_banner_and_forecast_markers() -> None:
    board = kanban_board()
    figure = gantt(build_context(plan_of(board), [board], SETTINGS))
    texts = [a.text for a in figure.layout.annotations]

    assert any(t.startswith("⚠ Sequence view:") for t in texts)
    assert {(s.name or "")[:3] for s in figure.layout.shapes if s.type == "line"} >= {"P50", "P85"}


def test_legend_shows_only_present_elements(board: BoardSnapshot, plan: Plan) -> None:
    scrum = legend(gantt(context(board, plan)))
    kanban_plan = kanban_board()
    kanban = legend(gantt(build_context(plan_of(kanban_plan), [kanban_plan], SETTINGS)))

    assert scrum >= {"Task", "Critical task", "Progress", "Summary", "Milestone", "Dependency"}
    assert scrum >= {"Today", "Nonworking time", "Late (due date)"}
    assert "Milestone" not in kanban
    assert "Late (due date)" not in kanban


def test_hover_reads_like_the_card_and_hides_internal_values(
    board: BoardSnapshot, plan: Plan
) -> None:
    figure = gantt(context(board, plan))
    card = next(h for h in trace(figure, "Critical task").hovertext if "ABC-109" in h)
    text = " ".join(hovers(figure))

    assert card.startswith("<b>ABC-109</b> · Payment API retries and idempotency keys")
    assert "Resource: Alice" in card
    assert "5 working days (5 SP → 5d)" in card
    assert "Starts after ABC-10" in card
    assert "Slack: 0 days · Status: To Do · 0% complete" in card
    assert "Due Oct 30 – " in card
    assert not [v for v in INTERNAL_VALUES if v in text]


def test_hover_and_table_escape_jira_text() -> None:
    # Tasks arrive with markup stripped (UntrustedText); what remains is escaped for Plotly.
    board = snapshot((task(1, summary=UntrustedText("R&D <b>bold</b> work"), story_points=1.0),))
    figure = gantt(build_context(plan_of(board), [board], SETTINGS))
    hover, table = " ".join(hovers(figure)), " ".join(trace(figure, "Task name").text)

    assert "R&amp;D bold work" in hover
    assert "R&amp;D bold work" in table
    assert "R&D" not in hover + table


# ---------------------------------------------------------------- filters and scale (§6.6, §8)


def test_filtered_view_marks_hidden_predecessors(board: BoardSnapshot, plan: Plan) -> None:
    ctx = build_context(plan, [board], SETTINGS, FILTERED_VIEW)
    figure = gantt(ctx)
    rows, _ = build_rows(ctx, plan.scheduled_tasks)
    stubs = [s.label.text for s in figure.layout.shapes if s.type == "line" and s.label.text]

    assert {r.task.lane for r in rows if r.task} == {AccountId("carol")}
    assert all(r.name.endswith("(filtered)") for r in rows if r.kind is RowKind.SUMMARY)
    assert "← ABC-105 (hidden)" in stubs
    assert rows[-1].kind is RowKind.MILESTONE  # its predecessor is hidden: listed last
    assert len(plan.scheduled_tasks) == 9  # the schedule itself is unchanged


def test_empty_filter_says_so(board: BoardSnapshot, plan: Plan) -> None:
    figure = gantt(context(board, plan, assignees=(AccountId("nobody"),)))

    assert any("No tasks match this filter" in a.text for a in figure.layout.annotations)


def test_color_by_epic_is_stable_under_filters(board: BoardSnapshot, plan: Plan) -> None:
    every = gantt(context(board, plan, color_by=ColorBy.EPIC))
    carol = gantt(context(board, plan, color_by=ColorBy.EPIC, assignees=(AccountId("carol"),)))
    palette = PALETTES[ThemeMode.LIGHT].categorical

    colors = {
        t.name: t.marker.color for t in every.data if t.type == "bar" and t.name[:4] == "ABC-"
    }
    assert colors == {"ABC-90 Payments": palette[0], "ABC-91 Search": palette[1]}
    assert all(
        colors[t.name] == t.marker.color for t in carol.data if t.type == "bar" and t.name in colors
    )


@pytest.fixture(scope="module")
def large() -> ReportContext:
    board = large_board()
    return build_context(plan_of(board), [board], SETTINGS)


def test_large_plans_start_collapsed(large: ReportContext) -> None:
    rows, collapsed = build_rows(large, large.plan.scheduled_tasks)
    banners = [a.text for a in gantt(large).layout.annotations if a.text.startswith("⚠")]

    assert len(large.plan.scheduled_tasks) == 200
    assert collapsed
    assert [r.kind for r in rows] == [RowKind.SUMMARY] * 8
    assert sum(r.children for r in rows) == 200
    assert not any("people" in b for b in banners)  # exactly 20: no filter hint yet
    assert any("200 tasks" in b for b in banners)


# ---------------------------------------------------------------- theme and determinism (§7, §10)


def _contrast(a: str, b: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_palette_check_in_both_modes(mode: ThemeMode) -> None:
    palette = PALETTES[mode]

    assert _contrast(palette.task, palette.surface) >= 3
    assert _contrast(palette.critical, palette.surface) >= 3
    assert _contrast(palette.text, palette.surface) >= 4.5
    assert _contrast(palette.text_secondary, palette.surface) >= 4.5
    assert len(palette.categorical) == 7


def test_same_plan_gives_byte_identical_figure_json(board: BoardSnapshot, plan: Plan) -> None:
    first = gantt(context(board, plan)).to_json()
    second = gantt(context(scrum_board(), plan_of(scrum_board()))).to_json()

    assert first == second
