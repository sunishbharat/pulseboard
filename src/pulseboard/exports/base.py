"""The ``Exporter`` protocol, its document model and the registry (entry-point group
``pulseboard.exporters``). Exporters never compute business numbers; they only render."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from pulseboard.domain.plan import Plan
from pulseboard.domain.types import ExportFormat, ReportId
from pulseboard.reports.base import Cell, ReportResult

ENTRY_POINT_GROUP = "pulseboard.exporters"


class PlotlyLibrary(StrEnum):
    """How an HTML export includes the Plotly library (DESIGN §14.2)."""

    INLINE = "inline"
    """Self-contained, works offline."""
    CDN = "cdn"
    """The pinned version from Plotly's CDN, with subresource integrity; needs internet."""
    SHARED = "shared"
    """One library file shared by several reports: next to the exports locally, or served by the
    hosted server at a fixed URL."""


@dataclass(frozen=True)
class ExportOptions:
    """Format settings an exporter may honor."""

    library: PlotlyLibrary = PlotlyLibrary.INLINE
    dark: bool = False
    """The figure is in the dark theme (and no light variant exists), so the page is dark too."""
    library_url: str | None = None
    """``shared`` only: where the library is served (hosted mode). No file is written then."""


DEFAULT_OPTIONS = ExportOptions()


@dataclass(frozen=True)
class Rendered:
    """Bytes of an export and how to serve them."""

    content: bytes
    media_type: str
    extension: str
    assets: tuple[tuple[str, bytes], ...] = ()
    """Files the export refers to, written next to it once (the shared Plotly library)."""


class Exporter(Protocol):
    """An export format."""

    @property
    def format(self) -> ExportFormat:
        """The export format this exporter produces."""
        ...

    def render(
        self,
        result: ReportResult,
        plan: Plan,
        generated_at: datetime,
        options: ExportOptions = DEFAULT_OPTIONS,
    ) -> Rendered:
        """Render one report of a plan."""
        ...


class TableDocument(BaseModel):
    """A table in an exported document."""

    model_config = ConfigDict(frozen=True)

    name: str
    columns: list[str]
    rows: list[list[Cell]]


class ReportDocument(BaseModel):
    """The JSON form of a report."""

    model_config = ConfigDict(frozen=True)

    report_id: ReportId
    title: str
    plan_id: str
    plan_date: date
    generated_at: datetime
    summary: list[str]
    tables: list[TableDocument]


def document(result: ReportResult, plan: Plan, generated_at: datetime) -> ReportDocument:
    """The report as a serializable document."""
    return ReportDocument(
        report_id=result.report_id,
        title=result.title,
        plan_id=plan.plan_id,
        plan_date=plan.plan_date,
        generated_at=generated_at,
        summary=list(result.summary),
        tables=[
            TableDocument(name=t.name, columns=list(t.columns), rows=[list(r) for r in t.rows])
            for t in result.tables
        ],
    )


def load_exporters() -> dict[ExportFormat, Exporter]:
    """Every exporter registered in the ``pulseboard.exporters`` entry-point group."""
    found: dict[ExportFormat, Exporter] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        exporter = entry.load()
        found[exporter.format] = exporter
    return dict(sorted(found.items()))
