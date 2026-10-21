"""Render the four Gantt visual review fixtures (Addendum 02 §10) and screenshot them.

Each fixture is planned with the real engine, rendered by the HTML exporter, and captured by a
headless Chrome or Edge at 1280 and 1920 px width. No Python dependency is added: the browser is
found on the machine (or given with ``--browser``).

Usage:
    uv run python scripts/render_gantt_review.py [--out build/gantt-review] [--browser PATH]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # the fixtures are shared with the tests

from gantt_fixtures import REVIEW_FIXTURES  # noqa: E402
from planning import SETTINGS, plan_of  # noqa: E402
from pulseboard.domain.types import ExportFormat, ReportId  # noqa: E402
from pulseboard.exports.base import ExportOptions, load_exporters  # noqa: E402
from pulseboard.reports.base import build_context, load_reports  # noqa: E402
from pulseboard.reports.theme import ThemeMode  # noqa: E402

WIDTHS = (1280, 1920)
PAGE_EXTRA_PX = 160
"""Summary lines, the collapsed table view and the footer around the chart."""
GENERATED_AT = datetime(2026, 10, 19, 9, tzinfo=UTC)
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "microsoft-edge",
)


def find_browser(explicit: str | None) -> str:
    """A Chromium-based browser that can take headless screenshots."""
    for candidate in (explicit, os.environ.get("PULSEBOARD_BROWSER"), *BROWSERS):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    msg = "No Chrome or Edge found; pass --browser or set PULSEBOARD_BROWSER."
    raise SystemExit(msg)


def render_html(name: str, theme: ThemeMode) -> tuple[bytes, int]:
    """The fixture's HTML report and the chart height in pixels."""
    board_factory, options = REVIEW_FIXTURES[name]
    board = board_factory()
    context = build_context(plan_of(board), [board], SETTINGS, replace(options, theme=theme))
    result = load_reports()[ReportId.GANTT].build(context)
    if result.figure is None:
        msg = f"The Gantt report of {name} has no figure."
        raise SystemExit(msg)
    page_options = ExportOptions(dark=theme is ThemeMode.DARK)
    exporter = load_exporters()[ExportFormat.HTML]
    rendered = exporter.render(result, context.plan, GENERATED_AT, page_options)
    return rendered.content, int(result.figure.layout.height)


def screenshot(browser: str, page: Path, out: Path, width: int, height: int) -> None:
    """Capture ``page`` at ``width`` × ``height`` CSS pixels."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "--virtual-time-budget=10000",
                f"--screenshot={out}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "gantt-review")
    parser.add_argument("--browser", default=None)
    parser.add_argument("--dark", action="store_true", help="also capture the dark theme")
    args = parser.parse_args(argv)
    browser = find_browser(args.browser)
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    themes = (ThemeMode.LIGHT, ThemeMode.DARK) if args.dark else (ThemeMode.LIGHT,)
    written = []
    for name in REVIEW_FIXTURES:
        for theme in themes:
            content, height = render_html(name, theme)
            suffix = "" if theme is ThemeMode.LIGHT else "-dark"
            page = out / f"{name}{suffix}.html"
            page.write_bytes(content)
            for width in WIDTHS:
                image = out / f"{name}{suffix}-{width}.png"
                screenshot(browser, page, image, width, height + PAGE_EXTRA_PX)
                written.append(str(image))
    sys.stdout.write(json.dumps(written, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
