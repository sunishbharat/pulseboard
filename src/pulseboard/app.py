"""Wiring: settings → store → sources → PlanService → MCP server (local mode, DESIGN §17.2)."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastmcp import FastMCP

from pulseboard import __version__
from pulseboard.credentials import Keychain, SystemKeychain, resolve_credentials
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.warnings import load_rules
from pulseboard.exports.base import load_exporters
from pulseboard.mcp.server import build_server
from pulseboard.reports.base import load_reports
from pulseboard.services.exports import DirectoryFiles, ExportService
from pulseboard.services.plan_service import PlanService
from pulseboard.settings import Settings
from pulseboard.sites import Site, SiteRegistry
from pulseboard.sources.base import BoardSource, SourceFactory, load_source_factories
from pulseboard.store.repository import SnapshotStore, create_store_engine, migrate

DATABASE_FILE = "pulseboard.db"
EXPORTS_DIR = "exports"
JIRA_SOURCE = "jira"


def utc_now() -> datetime:
    """The wall clock, in UTC. Only the service layer reads it; the engine never does."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class App:
    """The assembled application."""

    settings: Settings
    store: SnapshotStore
    service: PlanService
    exports: ExportService
    server: FastMCP


def open_store(settings: Settings, url: str | None = None) -> SnapshotStore:
    """The migrated snapshot store (SQLite in the data directory by default)."""
    if url is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{settings.data_dir / DATABASE_FILE}"
    engine = create_store_engine(url)
    migrate(engine)
    return SnapshotStore(engine)


def create_app(
    settings: Settings,
    *,
    store: SnapshotStore | None = None,
    source_factory: SourceFactory | None = None,
    clock: Callable[[], datetime] = utc_now,
    environ: Mapping[str, str] = os.environ,
    keychain: Keychain | None = None,
) -> App:
    """Assemble the app. Sources get credentials only when a site is first used."""
    store = store or open_store(settings)
    factory = source_factory or load_source_factories()[JIRA_SOURCE]
    chain = keychain if keychain is not None else SystemKeychain()

    def source_for(site: Site) -> BoardSource:
        return factory(site, resolve_credentials(site, environ, chain), settings)

    service = PlanService(
        settings=settings,
        registry=SiteRegistry(settings.sites),
        store=store,
        source_for=source_for,
        scheduler=PyJobShopScheduler(),
        rules=load_rules(),
        clock=clock,
        engine_version=__version__,
    )
    exports = ExportService(
        plans=service,
        store=store,
        reports=load_reports(),
        exporters=load_exporters(),
        files=DirectoryFiles(settings.data_dir / EXPORTS_DIR),
        retention=timedelta(days=settings.retention.exports_days),
        clock=clock,
    )
    return App(
        settings=settings,
        store=store,
        service=service,
        exports=exports,
        server=build_server(service, exports),
    )
