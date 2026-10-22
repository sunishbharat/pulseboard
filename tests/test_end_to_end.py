"""Full pipeline on the fixture board: Jira source (cassette) → PlanService → pb_build_plan →
pb_render → interactive HTML Gantt (DESIGN §9.6, §12, §14)."""

import json
import re
from pathlib import Path
from typing import Any

from fastmcp import Client

from fakes import ENV, LINK, Clock, FakeSource, T
from gantt_fixtures import large_board
from pulseboard.app import create_app, open_store
from pulseboard.domain.types import IssueId
from pulseboard.settings import Settings, TeamSettings
from pulseboard.sites import Site
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
    stored = results["stored"]
    assert isinstance(stored, dict)
    # The exact warning set: every data problem in the fixture is explained (PB-37).
    assert sorted({w["rule_id"] for w in stored["warnings"]}) == [
        "W002_external_blocker",  # ABC-108 waits for XYZ-7, not on the board
        "W003_missing_estimate", "W004_invalid_estimate", "W005_unassigned",
        "W006_due_date_miss", "W007_sprint_spill", "W010_milestone_risk", "W014_aging_wip",
    ]  # fmt: skip
    assert plan["warning_count"] == len(stored["warnings"])
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
    # One line per dependency plus one per milestone's predecessor; nothing is collapsed, so
    # nothing is left undrawn and no banner is needed (PB-37).
    milestones = [m for m in stored["milestones"] if m["forecast_date"]]
    assert len(_link_paths(figure)) == len(stored["dependency_edges"]) + len(milestones)
    assert not [a for a in layout.get("annotations", []) if a["text"].startswith("⚠")]
    resources = next(t for t in data if t["name"] == "Resource")["text"]
    assert {"Alice", "Bob", "Carol"} <= set(resources)  # display names, not account IDs


def _link_paths(figure: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        s
        for s in figure["layout"]["shapes"]
        if s.get("legendgroup") == "links" and s["type"] == "path" and "xsizemode" not in s
    ]


async def test_changed_link_types_apply_on_the_next_request(tmp_path: Path) -> None:
    """Same data folder, so the stored snapshot (built with "Blocks") is still fresh."""
    blocks = await render(tmp_path / "blocks.html", tmp_path / "data")
    relates = await render(
        tmp_path / "relates.html",
        tmp_path / "data",
        TeamSettings.model_validate(
            {"dependency_link_types": [{"name": "Relates", "blocker": "inward"}]}
        ),
    )

    def links(name: str) -> int:
        page = (tmp_path / name).read_text(encoding="utf-8")
        return len(_link_paths(json.loads(_figure_json(page))))

    assert blocks["plan"]["plan_id"] != relates["plan"]["plan_id"]  # type: ignore[index]
    assert links("blocks.html") - links("relates.html") == 5  # the fixture's five "Blocks" links


async def test_collapsed_gantt_links_groups_and_expands(cloud_site: Site, tmp_path: Path) -> None:
    """200 tasks start collapsed: one line for the only cross-epic link; expand=true shows all."""
    board = large_board()
    cross = [
        t.model_copy(update={"blocked_by": (IssueId(1000), *t.blocked_by)})
        if t.issue_id == 1009
        else t
        for t in board.tasks
    ]  # ABC-1009 (second epic) also waits for ABC-1000 (first epic)
    board = board.model_copy(update={"tasks": tuple(cross)})
    settings = Settings(sites=(cloud_site,), data_dir=tmp_path)
    clock = Clock()
    clock.now = T
    app = create_app(
        settings,
        store=open_store(settings, "sqlite://"),
        source_factory=lambda *_: FakeSource([board]),
        clock=clock,
        environ=ENV,
        keychain=None,
    )
    async with Client(app.server) as client:
        built = await client.call_tool("pb_build_plan", {"board_link": LINK})
        plan_id = (built.structured_content or {})["plan_id"]
        stored = await client.read_resource(f"pulseboard://plans/{plan_id}")
        edges = json.loads(getattr(stored[0], "text", ""))["dependency_edges"]
        figures = {}
        for expand in (False, True):
            rendered = await client.call_tool(
                "pb_render", {"plan_id": plan_id, "report_id": "gantt", "expand": expand}
            )
            uri = (rendered.structured_content or {})["uri"]
            page = getattr((await client.read_resource(uri))[0], "text", "")
            figures[expand] = json.loads(_figure_json(page))

    def banners(figure: dict[str, Any]) -> list[str]:
        return [a["text"] for a in figure["layout"]["annotations"] if a["text"].startswith("⚠")]

    assert len(_link_paths(figures[False])) == 1
    assert any(
        f"1 link line between groups for {len(edges)} dependencies" in b and "expand=true" in b
        for b in banners(figures[False])
    )
    assert len(_link_paths(figures[True])) == len(edges)
    assert not any("collapsed" in b for b in banners(figures[True]))


def _figure_json(page: str) -> str:
    """The figure embedded in the page for Plotly.newPlot."""
    match = re.search(
        r'<script type="application/json" id="figure-light"[^>]*>(.*?)</script>', page, re.S
    )
    assert match is not None
    return match.group(1)
