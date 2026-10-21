"""Export store: generated file names, owner directories, resources, retention (DESIGN §12.3)."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from fakes import LINK, Clock
from pulseboard.app import App
from pulseboard.cli import purge
from pulseboard.domain.types import ExportFormat, PlanId, ReportId
from pulseboard.errors import BoardNotFoundError, PlanExpiredError
from pulseboard.exports.base import PlotlyLibrary
from pulseboard.exports.formats import library_file_name
from pulseboard.services.exports import RenderOptions, Theme, owner_directory


async def build(app: App) -> PlanId:
    result = await app.service.build(LINK)
    assert result.plan is not None
    return result.plan.plan_id


async def test_render_writes_a_generated_file_name(app: App, tmp_path: Path) -> None:
    plan_id = await build(app)
    export = app.exports.render(plan_id, ReportId.GANTT, ExportFormat.HTML)
    record = export.record
    files = list((tmp_path / "exports").rglob("*.*"))

    assert record.resource_uri == f"pulseboard://exports/{record.export_id}"
    assert [f.name for f in files] == [f"{record.export_id}.html"]
    assert files[0].parent.relative_to(tmp_path / "exports").as_posix() == owner_directory(
        "acme", "acc-1"
    )
    assert record.size_bytes == files[0].stat().st_size
    assert record.expires - record.created == timedelta(days=7)
    assert len(export.summary) == 3


def test_owner_directory_never_contains_raw_account_ids() -> None:
    directory = owner_directory("acme", "557058:../../etc")

    assert directory.startswith("acme/")
    assert ":" not in directory
    assert ".." not in directory


async def test_read_back_and_unknown_exports(app: App) -> None:
    plan_id = await build(app)
    record = app.exports.render(plan_id, ReportId.THROUGHPUT, ExportFormat.CSV).record

    stored, content = app.exports.read(record.export_id)
    assert stored == record
    assert content.decode().startswith("week_start,done")
    with pytest.raises(PlanExpiredError):
        app.exports.read("0" * 32)


async def test_missing_report_or_format(app: App) -> None:
    plan_id = await build(app)

    with pytest.raises(BoardNotFoundError, match="format 'pdf'"):
        app.exports.render(plan_id, ReportId.GANTT, ExportFormat.PDF)
    with pytest.raises(BoardNotFoundError, match="report 'raid_log'"):
        app.exports.render(plan_id, ReportId.RAID_LOG, ExportFormat.HTML)
    with pytest.raises(PlanExpiredError):
        app.exports.render(PlanId("unknown"), ReportId.GANTT, ExportFormat.HTML)


async def test_seven_day_retention_purge(app: App, tmp_path: Path, clock: Clock) -> None:
    plan_id = await build(app)
    record = app.exports.render(plan_id, ReportId.GANTT, ExportFormat.JSON).record
    path = next((tmp_path / "exports").rglob(f"{record.export_id}.json"))

    clock.now += timedelta(days=6, hours=23)
    assert app.exports.read(record.export_id)[0] == record
    clock.now += timedelta(hours=2)
    with pytest.raises(PlanExpiredError):
        app.exports.read(record.export_id)  # expired exports are no longer served

    message = purge(app.settings, None, None, store=app.store, now=clock.now)
    assert "1 export files" in message
    assert not path.exists()


async def test_shared_library_is_written_once_next_to_the_exports(app: App, tmp_path: Path) -> None:
    plan_id = await build(app)
    shared = RenderOptions(library=PlotlyLibrary.SHARED)
    for report in (ReportId.GANTT, ReportId.WORKLOAD):
        app.exports.render(plan_id, report, ExportFormat.HTML, shared)
    files = sorted(f.name for f in (tmp_path / "exports").rglob("*.*"))

    assert files.count(library_file_name()) == 1
    assert len(files) == 3


async def test_auto_theme_embeds_both_figures_only_in_html(app: App) -> None:
    plan_id = await build(app)
    auto = app.exports.render(plan_id, ReportId.GANTT, ExportFormat.HTML)
    dark = app.exports.render(
        plan_id, ReportId.GANTT, ExportFormat.HTML, RenderOptions(theme=Theme.DARK)
    )

    assert 'id="figure-dark"' in app.exports.read(auto.record.export_id)[1].decode()
    page = app.exports.read(dark.record.export_id)[1].decode()
    assert 'id="figure-dark"' not in page
    assert 'data-theme="dark"' in page
    assert re.search(r"\"paper_bgcolor\": ?\"#1a1a19\"", page)  # the figure is dark too
