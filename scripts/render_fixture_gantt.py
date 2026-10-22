"""Render the fixture board's Gantt chart through the full pipeline, offline.

Jira source (replaying the hand-built Cloud cassette, no network) → snapshot store → PlanService
→ MCP ``pb_build_plan`` → MCP ``pb_render`` → the export resource → an HTML file.

Usage:
    uv run python scripts/render_fixture_gantt.py [--out fixture-gantt.html]
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import vcr
from fastmcp import Client

from pulseboard.app import create_app, open_store
from pulseboard.credentials import Credentials
from pulseboard.domain.types import AuthMethod, Deployment, SiteId
from pulseboard.settings import Limits, Settings, TeamSettings
from pulseboard.sites import Site
from pulseboard.sources.base import BoardSource
from pulseboard.sources.jira.client import JiraBoardSource

ROOT = Path(__file__).resolve().parents[1]
CASSETTE = ROOT / "tests" / "fixtures" / "cassettes" / "cloud_board.yaml"
BOARD_LINK = "https://acme.atlassian.net/jira/software/projects/ABC/boards/42"
FIXTURE_NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
"""The cassette was built for this moment (reporting window from 2026-07-21)."""
CASSETTE_PAGE_SIZE = 5
FAKE_ENV = {"PULSEBOARD_ACME_EMAIL": "fixture@example.com", "PULSEBOARD_ACME_TOKEN": "x"}


def fixture_settings(data_dir: Path, team: TeamSettings | None = None) -> Settings:
    """Settings for the fixture site."""
    site = Site(
        site_id=SiteId("acme"),
        base_url="https://acme.atlassian.net",
        deployment=Deployment.CLOUD,
        auth_method=AuthMethod.API_TOKEN,
    )
    return Settings(
        sites=(site,),
        data_dir=data_dir,
        team=team or TeamSettings(),
        limits=Limits(jira_page_size=CASSETTE_PAGE_SIZE),
    )


def replaying_source(site: Site, credentials: Credentials, settings: Settings) -> BoardSource:
    """The real Jira source, without the HTTP cache (cassette replies can't be cached)."""
    return JiraBoardSource(site, credentials, settings, cache_enabled=False)


async def render(out: Path, data_dir: Path, team: TeamSettings | None = None) -> dict[str, object]:
    """Run the pipeline and write the Gantt HTML to ``out``. Returns the tool results."""
    settings = fixture_settings(data_dir, team)
    app = create_app(
        settings,
        store=open_store(settings),
        source_factory=replaying_source,
        clock=lambda: FIXTURE_NOW,
        environ=FAKE_ENV,
        keychain=None,
    )
    replay = vcr.VCR(
        record_mode="none", match_on=["method", "scheme", "host", "port", "path", "query"]
    )
    with replay.use_cassette(str(CASSETTE)):
        async with Client(app.server) as client:
            built = await client.call_tool("pb_build_plan", {"board_link": BOARD_LINK})
            plan = built.structured_content or {}
            rendered = await client.call_tool(
                "pb_render", {"plan_id": plan["plan_id"], "report_id": "gantt", "format": "html"}
            )
            export = rendered.structured_content or {}
            contents = await client.read_resource(str(export["uri"]))
            stored = await client.read_resource(f"pulseboard://plans/{plan['plan_id']}")
    out.write_text(getattr(contents[0], "text", ""), encoding="utf-8")
    return {
        "plan": plan,
        "export": export,
        "stored": json.loads(getattr(stored[0], "text", "{}")),
    }


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("fixture-gantt.html"))
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        results = asyncio.run(render(args.out, Path(data_dir)))
    plan = results["plan"]
    sys.stdout.write(json.dumps(plan, indent=1, default=str) + "\n")
    sys.stdout.write(f"Gantt written to {args.out.resolve()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
