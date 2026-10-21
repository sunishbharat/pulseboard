"""The Pulseboard Plotly theme: ``pulseboard_light`` and ``pulseboard_dark`` (Addendum 02 §5, §7).

Colors live only here. Every chart applies one of the two registered templates; the Gantt also
reads the bar, line and surface tokens of ``Palette`` for its shapes.
"""

from dataclasses import dataclass
from enum import StrEnum

import plotly.graph_objects as go
import plotly.io as pio

FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'


class ThemeMode(StrEnum):
    """A rendered color mode (``auto`` is resolved by the exporter)."""

    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class Palette:
    """The color tokens of one mode."""

    task: str
    critical: str
    progress: str
    critical_progress: str
    done: str
    summary: str
    milestone: str
    link: str
    late: str
    nonworking: str
    grid: str
    header: str
    surface: str
    text: str
    text_secondary: str
    text_muted: str
    categorical: tuple[str, ...]
    """The validated 7-slot palette for ``color_by = epic``, in fixed order."""


# Task/critical pairs and the categorical palette validated 2026-10-21 (Addendum 02 §5).
PALETTES: dict[ThemeMode, Palette] = {
    ThemeMode.LIGHT: Palette(
        task="#2a78d6",
        critical="#d03b3b",
        progress="#0d366b",
        critical_progress="#5e1717",
        done="#a3a29b",
        summary="#3b3b39",
        milestone="#0b0b0b",
        link="#52514e",
        late="#ec835a",
        nonworking="#efeee9",
        grid="#e1e0d9",
        header="#f6f6f3",
        surface="#fcfcfb",
        text="#0b0b0b",
        text_secondary="#52514e",
        text_muted="#898781",
        categorical=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"),
    ),
    ThemeMode.DARK: Palette(
        task="#3987e5",
        critical="#e66767",
        progress="#0d1a33",
        critical_progress="#3d0f0f",
        done="#5c5c58",
        summary="#c3c2b7",
        milestone="#f0efec",
        link="#c3c2b7",
        late="#ec835a",
        nonworking="#242422",
        grid="#2c2c2a",
        header="#20201e",
        surface="#1a1a19",
        text="#f0efec",
        text_secondary="#c3c2b7",
        text_muted="#898781",
        categorical=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"),
    ),
}


def template_name(mode: ThemeMode) -> str:
    """The registered Plotly template name of a mode."""
    return f"pulseboard_{mode.value}"


def _template(palette: Palette) -> go.layout.Template:
    axis = {
        "gridcolor": palette.grid,
        "linecolor": palette.grid,
        "zerolinecolor": palette.grid,
        "tickcolor": palette.grid,
        "tickfont": {"color": palette.text_secondary, "size": 12},
        "title": {"font": {"color": palette.text_secondary, "size": 12}},
    }
    template = go.layout.Template(pio.templates["simple_white"])
    template.layout.update(
        font={"family": FONT_FAMILY, "size": 12, "color": palette.text},
        title={"font": {"size": 18, "color": palette.text}},
        paper_bgcolor=palette.surface,
        plot_bgcolor=palette.surface,
        colorway=list(palette.categorical),
        xaxis=axis,
        yaxis=axis,
        legend={"font": {"color": palette.text_secondary, "size": 12}, "bgcolor": palette.surface},
        hoverlabel={
            "bgcolor": palette.surface,
            "bordercolor": palette.grid,
            "font": {"family": FONT_FAMILY, "size": 12, "color": palette.text},
            "align": "left",
        },
        shapedefaults={"line": {"color": palette.link, "width": 1}},
        annotationdefaults={"font": {"color": palette.text_secondary, "size": 12}},
    )
    return template


for _mode, _palette in PALETTES.items():
    pio.templates[template_name(_mode)] = _template(_palette)
