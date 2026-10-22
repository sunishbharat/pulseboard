"""HTML (Jinja2 + embedded Plotly), XLSX (pandas/openpyxl), CSV (pandas) and JSON exporters."""

import base64
import hashlib
import io
import secrets
import zipfile
from dataclasses import dataclass
from datetime import datetime
from functools import cache

import pandas as pd
import plotly.graph_objects as go
from jinja2 import PackageLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from plotly.offline import get_plotlyjs, get_plotlyjs_version

from pulseboard.domain.plan import Plan
from pulseboard.domain.types import ExportFormat
from pulseboard.exports.base import (
    DEFAULT_OPTIONS,
    ExportOptions,
    PlotlyLibrary,
    Rendered,
    document,
)
from pulseboard.reports.base import ReportResult, Table

TEMPLATE = "report.html.j2"
_JSON_ESCAPES = str.maketrans({c: f"\\u{ord(c):04x}" for c in "<>&/"})
"""Keeps embedded figure JSON from closing its script element; JSON decodes them back."""
CHART_SCRIPT = """(function () {
  var dark = document.getElementById("figure-dark");
  var useDark = dark !== null && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var source = document.getElementById(useDark ? "figure-dark" : "figure-light");
  var figure = JSON.parse(source.textContent);
  var svg = {name: "Download SVG", icon: Plotly.Icons.disk, click: function (gd) {
    Plotly.downloadImage(gd, {format: "svg", filename: "pulseboard-chart"});
  }};
  var config = {displaylogo: false, responsive: true, scrollZoom: true,
    toImageButtonOptions: {format: "png", filename: "pulseboard-chart", scale: 2},
    modeBarButtons: [["toImage", svg], ["zoom2d", "pan2d", "resetScale2d"]]};
  Plotly.newPlot("chart", figure.data, figure.layout, config).then(function (gd) {
    gd.on("plotly_click", function (event) {
      var url = event.points.length ? event.points[0].customdata : null;
      if (typeof url === "string" && url.indexOf("https://") === 0) {
        window.open(url, "_blank", "noopener");
      }
    });
  });
})();"""
"""Draws the chart in the viewer's color scheme with the reduced mode bar (Addendum 02 §6.10);
a click on a task bar opens its Jira issue."""
_SHEET_NAME_MAX = 31
_SUMMARY_SHEET = "summary"
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
"""Leading characters that make a spreadsheet read a cell as a formula (ADDENDUM-03 §8)."""
_ENVIRONMENT = SandboxedEnvironment(
    loader=PackageLoader("pulseboard.exports", "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
)


def content_security_policy(nonce: str) -> str:
    """CSP for HTML reports: only scripts carrying this file's nonce run (the inline chart code
    and, for ``cdn`` / ``shared``, the pinned Plotly library); nothing else is fetched."""
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; "
        "base-uri 'none'; form-action 'none'"
    )


def spreadsheet_safe(value: object) -> object:
    """A CSV text cell that cannot run as a formula: one starting with ``= + - @``, a tab or a
    carriage return gets a leading apostrophe, which spreadsheets read as plain text. Other
    values are unchanged (ADDENDUM-03 §8)."""
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGERS):
        return f"'{value}"
    return value


def _frame(table: Table) -> pd.DataFrame:
    return pd.DataFrame([list(r) for r in table.rows], columns=list(table.columns))


def _safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Every text cell and column name made spreadsheet-safe (CSV)."""
    safe = frame.map(spreadsheet_safe)
    safe.columns = [spreadsheet_safe(str(c)) for c in frame.columns]
    return safe


def _write_sheet(writer: pd.ExcelWriter, frame: pd.DataFrame, sheet: str) -> None:
    """Write a sheet with every text cell stored as a string, never as a formula. Workbooks
    keep cell types, so text keeps its exact value (no apostrophe, unlike CSV)."""
    frame.to_excel(writer, sheet_name=sheet, index=False)
    for row in writer.sheets[sheet].iter_rows():
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"


def figure_json(figure: go.Figure) -> str:
    """A figure as JSON that is safe inside a ``<script>`` element."""
    return str(figure.to_json()).translate(_JSON_ESCAPES)


@cache
def _library() -> tuple[str, str]:
    """The bundled Plotly library and its subresource-integrity hash."""
    source = get_plotlyjs()
    digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
    return source, f"sha256-{digest}"


def library_source() -> str:
    """The bundled Plotly library (served by the hosted server for ``shared`` exports)."""
    return _library()[0]


def library_file_name() -> str:
    """The shared library's file name; the version is pinned by the installed Plotly."""
    return f"plotly-{get_plotlyjs_version()}.min.js"


@dataclass(frozen=True)
class HtmlExporter:
    """Interactive HTML: the report template with the Plotly chart embedded."""

    format: ExportFormat = ExportFormat.HTML

    def render(
        self,
        result: ReportResult,
        plan: Plan,
        generated_at: datetime,
        options: ExportOptions = DEFAULT_OPTIONS,
    ) -> Rendered:
        """Render the HTML report."""
        nonce = secrets.token_urlsafe(16)
        source, integrity = _library()
        assets: tuple[tuple[str, bytes], ...] = ()
        if options.library is PlotlyLibrary.SHARED and options.library_url is None:
            assets = ((library_file_name(), source.encode()),)
        html = _ENVIRONMENT.get_template(TEMPLATE).render(
            csp=content_security_policy(nonce),
            nonce=nonce,
            title=result.title,
            plan=plan,
            generated_at=generated_at,
            summary=result.summary,
            show_header=not result.titled_figure,
            light=figure_json(result.figure) if result.figure is not None else None,
            dark=figure_json(result.dark_figure) if result.dark_figure is not None else None,
            theme=None if result.dark_figure is not None else ("dark" if options.dark else "light"),
            library=options.library.value,
            library_source=source,
            library_src=(
                f"https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"
                if options.library is PlotlyLibrary.CDN
                else options.library_url or library_file_name()
            ),
            integrity=integrity,
            chart_script=CHART_SCRIPT,
            tables=result.tables,
        )
        return Rendered(html.encode(), "text/html; charset=utf-8", "html", assets)


@dataclass(frozen=True)
class XlsxExporter:
    """Excel workbook: a summary sheet and one sheet per table."""

    format: ExportFormat = ExportFormat.XLSX

    def render(
        self,
        result: ReportResult,
        plan: Plan,
        generated_at: datetime,
        options: ExportOptions = DEFAULT_OPTIONS,
    ) -> Rendered:
        """Render the workbook."""
        buffer = io.BytesIO()
        summary = pd.DataFrame(
            {
                "field": [
                    "report",
                    "plan_id",
                    "plan_date",
                    "generated_at",
                    *(["summary"] * len(result.summary)),
                ],
                "value": [
                    result.title,
                    plan.plan_id,
                    str(plan.plan_date),
                    generated_at.isoformat(),
                    *result.summary,
                ],
            }
        )
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            _write_sheet(writer, summary, _SUMMARY_SHEET)
            for table in result.tables:
                _write_sheet(writer, _frame(table), table.name[:_SHEET_NAME_MAX])
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return Rendered(buffer.getvalue(), media, "xlsx")


@dataclass(frozen=True)
class CsvExporter:
    """CSV: one file per table; several tables come as a ZIP of CSV files."""

    format: ExportFormat = ExportFormat.CSV

    def render(
        self,
        result: ReportResult,
        plan: Plan,
        generated_at: datetime,
        options: ExportOptions = DEFAULT_OPTIONS,
    ) -> Rendered:
        """Render CSV (or a ZIP of CSVs)."""
        if len(result.tables) == 1:
            text = _safe_frame(_frame(result.tables[0])).to_csv(index=False, lineterminator="\n")
            return Rendered(text.encode(), "text/csv; charset=utf-8", "csv")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for table in result.tables:
                archive.writestr(
                    f"{table.name}.csv",
                    _safe_frame(_frame(table)).to_csv(index=False, lineterminator="\n"),
                )
        return Rendered(buffer.getvalue(), "application/zip", "zip")


@dataclass(frozen=True)
class JsonExporter:
    """JSON: the report document (Pydantic serialization)."""

    format: ExportFormat = ExportFormat.JSON

    def render(
        self,
        result: ReportResult,
        plan: Plan,
        generated_at: datetime,
        options: ExportOptions = DEFAULT_OPTIONS,
    ) -> Rendered:
        """Render JSON."""
        payload = document(result, plan, generated_at).model_dump_json(indent=1)
        return Rendered(payload.encode(), "application/json", "json")


html_exporter = HtmlExporter()
xlsx_exporter = XlsxExporter()
csv_exporter = CsvExporter()
json_exporter = JsonExporter()
