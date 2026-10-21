"""Full pipeline on the fixture board: Jira source (cassette) → PlanService → pb_build_plan →
pb_render → interactive HTML Gantt (DESIGN §9.6, §12, §14)."""

import json
import re
from pathlib import Path

from render_fixture_gantt import render


async def test_fixture_board_renders_an_interactive_gantt(tmp_path: Path) -> None:
    out = tmp_path / "gantt.html"

    results = await render(out, tmp_path / "data")

    plan = results["plan"]
    export = results["export"]
    assert isinstance(plan, dict)
    assert isinstance(export, dict)
    assert plan["open_issues"] == 9
    assert plan["solver_status"] == "optimal"
    assert {w["rule"] for w in plan["top_warnings"]} <= {
        "W001_cycle", "W002_external_blocker", "W003_missing_estimate", "W004_invalid_estimate",
        "W005_unassigned", "W006_due_date_miss", "W007_sprint_spill", "W010_milestone_risk",
    }  # fmt: skip
    assert str(export["uri"]).startswith("pulseboard://exports/")

    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Content-Security-Policy" in page
    assert "Plotly.newPlot" in page
    figure = json.loads(_figure_json(page))
    data, layout = figure["data"], figure["layout"]
    bars = [y for t in data if t["name"] in {"Task", "Critical task"} for y in t["y"]]
    assert len(bars) == 9  # one row per open task
    assert all(
        "browse/ABC-" in url for t in data if t["type"] == "bar" for url in t.get("customdata", [])
    )
    assert "Critical task" in {t["name"] for t in data}  # critical tasks are red bars
    table = next(t for t in data if t["name"] == "Task name")
    assert all("557058:" not in text for text in table["text"])
    shapes = layout["shapes"]
    assert any(s.get("name") == "Today" for s in shapes)
    assert any(s.get("name") == "Nonworking time" for s in shapes)
    assert any(s.get("legendgroup") == "links" for s in shapes)  # dependency lines
    resources = next(t for t in data if t["name"] == "Resource")["text"]
    assert {"Alice", "Bob", "Carol"} <= set(resources)  # display names, not account IDs


def _figure_json(page: str) -> str:
    """The figure embedded in the page for Plotly.newPlot."""
    match = re.search(
        r'<script type="application/json" id="figure-light"[^>]*>(.*?)</script>', page, re.S
    )
    assert match is not None
    return match.group(1)
