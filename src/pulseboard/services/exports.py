"""Export store: render a report, write it under a generated name, serve it as a resource.

Locally, files live in ``<data_dir>/exports/<site>/<account hash>/<export_id>.<ext>``; hosted
Profile A keeps them in memory (``pulseboard.hosted.memory``). Names come only from the export ID
and format, never from board names or any Jira text (DESIGN §12.3).
"""

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pulseboard.domain.plan import ExportRecord
from pulseboard.domain.types import AccountId, ExportFormat, ExportId, PlanId, ReportId
from pulseboard.errors import BoardNotFoundError, PlanExpiredError
from pulseboard.exports.base import Exporter, ExportOptions, PlotlyLibrary
from pulseboard.reports.base import (
    ChartOptions,
    ColorBy,
    GroupBy,
    ReportDefinition,
    ReportResult,
    build_context,
)
from pulseboard.reports.theme import ThemeMode
from pulseboard.services.plan_service import Owner, PlanService
from pulseboard.store.protocol import Store

RESOURCE_PREFIX = "pulseboard://exports/"
_EXPORT_ID_BYTES = 16
_OWNER_HASH_LENGTH = 16


class Theme(StrEnum):
    """Chart theme setting; ``auto`` follows the viewer's color scheme in HTML (DESIGN §14.2)."""

    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class RenderOptions:
    """Chart and export settings of one ``pb_render`` call."""

    group_by: GroupBy = GroupBy.EPIC
    color_by: ColorBy = ColorBy.TYPE
    theme: Theme = Theme.AUTO
    library: PlotlyLibrary = PlotlyLibrary.INLINE


DEFAULT_RENDER = RenderOptions()


@dataclass(frozen=True)
class Export:
    """A rendered export and its short summary."""

    record: ExportRecord
    summary: tuple[str, ...]


def owner_directory(site_id: str, account_id: str) -> str:
    """Per-(site, account) directory; the account ID is hashed because it may contain
    characters that are not valid in file names (for example ``:``)."""
    digest = hashlib.sha256(account_id.encode()).hexdigest()[:_OWNER_HASH_LENGTH]
    return f"{site_id}/{digest}"


class ExportFiles(Protocol):
    """Where export content lives, by path relative to the export root."""

    def write(self, relative: str, content: bytes) -> None:
        """Store an export's content."""
        ...

    def write_asset(self, relative: str, content: bytes) -> None:
        """Store a shared asset next to the exports, unless it is already there."""
        ...

    def read(self, relative: str) -> bytes | None:
        """An export's content, or ``None`` if it is gone."""
        ...


class DirectoryFiles:
    """Exports as files under a directory (local mode)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, relative: str) -> Path | None:
        root = self._root.resolve()
        path = (root / relative).resolve()
        return path if path.is_relative_to(root) else None

    def write(self, relative: str, content: bytes) -> None:
        """Write the file, creating its owner directory."""
        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def write_asset(self, relative: str, content: bytes) -> None:
        """Write the asset once; later exports reuse it."""
        target = self._root / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def read(self, relative: str) -> bytes | None:
        """The file's bytes; ``None`` if missing or outside the export directory."""
        path = self._path(relative)
        return path.read_bytes() if path is not None and path.is_file() else None


class ExportService:
    """Renders reports into the export store and reads them back."""

    def __init__(
        self,
        *,
        plans: PlanService,
        store: Store,
        reports: Mapping[ReportId, ReportDefinition],
        exporters: Mapping[ExportFormat, Exporter],
        files: ExportFiles,
        retention: timedelta,
        clock: Callable[[], datetime],
        owner: Owner | None = None,
        shared_library_url: str | None = None,
    ) -> None:
        """``owner`` (hosted mode) is the calling account; exports of others are never served.
        ``shared_library_url`` (hosted mode) is where ``shared`` HTML exports load Plotly from."""
        self._plans = plans
        self._store = store
        self._reports = dict(reports)
        self._exporters = dict(exporters)
        self._files = files
        self._retention = retention
        self._clock = clock
        self._owner = owner
        self._shared_library_url = shared_library_url

    @property
    def formats(self) -> tuple[ExportFormat, ...]:
        """Formats with a registered exporter."""
        return tuple(self._exporters)

    def render(
        self,
        plan_id: PlanId,
        report_id: ReportId,
        export_format: ExportFormat,
        options: RenderOptions = DEFAULT_RENDER,
    ) -> Export:
        """Render one report of a stored plan and store the file.

        Raises:
            PlanExpiredError: unknown or purged plan.
            BoardNotFoundError: no such report or format in this installation.
        """
        report = self._reports.get(report_id)
        exporter = self._exporters.get(export_format)
        if report is None or exporter is None:
            missing = f"report '{report_id}'" if report is None else f"format '{export_format}'"
            msg = f"No {missing} is available."
            raise BoardNotFoundError(msg, hint="Use a v1.0 report and html, xlsx, csv or json.")
        plan = self._plans.get_plan(plan_id)
        snapshots, settings = (
            self._plans.snapshots_for(plan),
            self._plans.settings_for(plan.site_id),
        )

        def build(theme: ThemeMode) -> ReportResult:
            chart = ChartOptions(theme=theme, group_by=options.group_by, color_by=options.color_by)
            return report.build(build_context(plan, snapshots, settings, chart))

        follows_viewer = options.theme is Theme.AUTO and export_format is ExportFormat.HTML
        dark = options.theme is Theme.DARK
        result = build(ThemeMode.DARK if dark else ThemeMode.LIGHT)
        if follows_viewer:
            result = replace(result, dark_figure=build(ThemeMode.DARK).figure)
        now = self._clock()
        export_options = ExportOptions(
            library=options.library, dark=dark, library_url=self._shared_library_url
        )
        rendered = exporter.render(result, plan, now, export_options)

        export_id = ExportId(secrets.token_hex(_EXPORT_ID_BYTES))
        directory = owner_directory(plan.site_id, plan.account_id)
        relative = f"{directory}/{export_id}.{rendered.extension}"
        self._files.write(relative, rendered.content)
        for name, content in rendered.assets:
            self._files.write_asset(f"{directory}/{name}", content)
        record = ExportRecord(
            export_id=export_id,
            site_id=plan.site_id,
            account_id=plan.account_id,
            report_id=report_id,
            format=export_format,
            media_type=rendered.media_type,
            size_bytes=len(rendered.content),
            created=now,
            expires=now + self._retention,
            resource_uri=f"{RESOURCE_PREFIX}{export_id}",
        )
        self._store.save_export(record, relative)
        return Export(record=record, summary=result.summary)

    def read(self, export_id: str) -> tuple[ExportRecord, bytes]:
        """An export's record and content.

        Raises:
            PlanExpiredError: unknown, expired or missing export, or (hosted mode) an export of
                another account, with the same message.
        """
        owner = self._owner
        return self.read_if(
            export_id, None if owner is None else lambda account: account == owner()
        )

    def read_if(
        self, export_id: str, is_owner: Callable[[AccountId], bool] | None
    ) -> tuple[ExportRecord, bytes]:
        """``read`` with an explicit owner check (signed download links, hosted mode).

        Raises:
            PlanExpiredError: unknown, expired or missing export, or ``is_owner`` refuses it.
        """
        stored = self._store.get_export(ExportId(export_id))
        content = self._files.read(stored.path) if stored is not None else None
        if (
            stored is None
            or content is None
            or stored.record.expires <= self._clock()
            or (is_owner is not None and not is_owner(stored.record.account_id))
        ):
            msg = "This export is unknown or has expired."
            raise PlanExpiredError(msg, hint="Call pb_render again.")
        return stored.record, content
