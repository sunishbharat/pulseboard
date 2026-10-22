"""The Profile A HTTP server (ADDENDUM-01 §1A.6): stateless streamable HTTP at ``/mcp``.

Per tool call or resource read, ``CallerMiddleware``:
1. checks any ``board_link`` against the one configured site (pure: a foreign host is rejected
   before the caller's token is used anywhere),
2. reads the caller's Jira credentials from the request headers (hard error if missing),
3. resolves the caller's Jira account (current-user endpoint, cached by salted hash),
4. runs the tool with that caller as the owner of every cache entry it reads or writes.

Routes: ``/mcp`` (MCP), ``/health`` (health check), ``/exports/{token}`` (signed downloads) and
``/static/plotly-<version>.min.js`` (the installed Plotly library for ``shared`` HTML exports).
Only ``/mcp`` needs the endpoint access key (``access.py``). Links use ``PULSEBOARD_PUBLIC_URL``,
never the request's host or scheme headers.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import anyio.to_thread
import mcp.types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as McpToolError
from fastmcp.resources import ResourceResult
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.middleware import Middleware as AsgiMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pulseboard import __version__
from pulseboard.app import utc_now
from pulseboard.cli.console import Console
from pulseboard.credentials import Credentials
from pulseboard.domain.plan import ExportRecord
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId, ExportId
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.scheduling import Scheduler
from pulseboard.engine.warnings import load_rules
from pulseboard.errors import ConfigError, NotAvailableError, PlanExpiredError, ToolError
from pulseboard.exports.base import load_exporters
from pulseboard.exports.formats import library_file_name, library_source
from pulseboard.hosted.access import HEALTH_PATH, STATIC_PATH, AccessKeyMiddleware
from pulseboard.hosted.caller import (
    Caller,
    caller_scope,
    credentials_from_headers,
    current_account,
    current_caller,
)
from pulseboard.hosted.config import HostedConfig, credential_variables, load_hosted_config
from pulseboard.hosted.identity import IdentityCache
from pulseboard.hosted.links import EXPORTS_PATH, ExportLinks
from pulseboard.hosted.logs import configure_logging
from pulseboard.hosted.memory import DEFAULT_LIMITS, MemoryFiles, MemoryLimits, MemoryStore
from pulseboard.mcp.server import build_server, error_text
from pulseboard.reports.base import load_reports
from pulseboard.services.exports import ExportService
from pulseboard.services.plan_service import PlanService
from pulseboard.services.solve_slots import SolveSlots
from pulseboard.settings import Settings
from pulseboard.sites import Site, SiteRegistry
from pulseboard.sources.base import BoardSource, Progress, SourceFactory
from pulseboard.sources.jira.client import JiraBoardSource

MCP_PATH = "/mcp"
OPEN_TOOLS = frozenset({"pb_list_sites"})
"""Tools that read only Pulseboard's configuration, never Jira."""
PDF_UNAVAILABLE = error_text(
    NotAvailableError(
        "PDF export is not available on this server.", hint="Use html, xlsx, csv or json."
    )
)
"""PDF export is deferred; HTTP mode answers before input validation and never loads a renderer."""
_STATIC_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",  # the file name carries the version
    "X-Content-Type-Options": "nosniff",
}
_DOWNLOAD_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def hosted_source_factory(site: Site, credentials: Credentials, settings: Settings) -> BoardSource:
    """Jira sources for HTTP mode: the HTTP cache stays in the per-call source's memory."""
    return JiraBoardSource(site, credentials, settings, persist_cache=False)


@dataclass(frozen=True)
class KnownAccountSource:
    """A source whose account was already resolved (no second current-user call)."""

    account: AccountId
    source: BoardSource

    def account_id(self) -> AccountId:
        """The caller's account, from the identity cache."""
        return self.account

    def settings_key(self) -> str:
        """The wrapped source's settings key."""
        return self.source.settings_key()

    def fetch_snapshot(
        self,
        board_id: BoardId,
        *,
        window_start: date,
        updated_since: datetime | None,
        fetched_at: datetime,
        progress: Progress | None = None,
    ) -> BoardSnapshot:
        """Fetch with the caller's own credentials."""
        return self.source.fetch_snapshot(
            board_id,
            window_start=window_start,
            updated_since=updated_since,
            fetched_at=fetched_at,
            progress=progress,
        )


class CallerMiddleware(Middleware):
    """Validates links, then binds the caller to each tool call and resource read."""

    def __init__(
        self,
        registry: SiteRegistry,
        site: Site,
        identities: IdentityCache,
        identify: Callable[[Credentials], AccountId],
    ) -> None:
        self._registry = registry
        self._site = site
        self._identities = identities
        self._identify = identify

    async def _caller(self) -> Caller:
        credentials = credentials_from_headers(get_http_headers(include_all=True), self._site)
        account = await anyio.to_thread.run_sync(
            lambda: self._identities.resolve(credentials, lambda: self._identify(credentials)),
            abandon_on_cancel=True,
        )
        return Caller(account_id=account, credentials=credentials)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Check the board link and the caller before the tool runs."""
        if context.message.name in OPEN_TOOLS:
            return await call_next(context)
        arguments = context.message.arguments or {}
        if context.message.name == "pb_render" and arguments.get("format") == "pdf":
            raise McpToolError(PDF_UNAVAILABLE)
        try:
            link = arguments.get("board_link")
            if isinstance(link, str):
                self._registry.match(link)
            caller = await self._caller()
        except ToolError as exc:
            raise McpToolError(error_text(exc)) from None
        with caller_scope(caller):
            return await call_next(context)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mcp.types.ReadResourceRequestParams],
        call_next: CallNext[mcp.types.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        """Resources (plans, exports) are served to their owner only."""
        try:
            caller = await self._caller()
        except ToolError as exc:
            raise McpToolError(error_text(exc)) from None
        with caller_scope(caller):
            return await call_next(context)


@dataclass(frozen=True)
class HostedApp:
    """The assembled Profile A application."""

    config: HostedConfig
    settings: Settings
    store: MemoryStore
    service: PlanService
    exports: ExportService
    links: ExportLinks
    solve_slots: SolveSlots
    server: FastMCP

    @property
    def asgi_middleware(self) -> list[AsgiMiddleware]:
        """The access key check, outermost."""
        return [AsgiMiddleware(AccessKeyMiddleware, keys=self.config.access_keys)]

    def http_app(self) -> StarletteWithLifespan:
        """The ASGI app: stateless streamable HTTP at ``/mcp`` plus the custom routes."""
        return self.server.http_app(
            path=MCP_PATH, stateless_http=True, middleware=self.asgi_middleware
        )

    def run(self) -> None:
        """Serve until stopped. Uvicorn uses the root logging set up by ``configure_logging``."""
        self.server.run(
            transport="http",
            host=self.config.host,
            port=self.config.port,
            path=MCP_PATH,
            stateless_http=True,
            middleware=self.asgi_middleware,
            show_banner=False,
            uvicorn_config={"log_config": None},
        )


def create_hosted_app(  # noqa: PLR0913 - the injected collaborators
    settings: Settings,
    config: HostedConfig,
    *,
    source_factory: SourceFactory = hosted_source_factory,
    clock: Callable[[], datetime] = utc_now,
    limits: MemoryLimits = DEFAULT_LIMITS,
    identities: IdentityCache | None = None,
    links: ExportLinks | None = None,
    scheduler: Scheduler | None = None,
) -> HostedApp:
    """Assemble Profile A: one site from ``config``, memory-only storage, per-call sources."""
    settings = settings.model_copy(update={"sites": (config.site,)})
    registry = SiteRegistry(settings.sites)
    store = MemoryStore(limits)
    identities = identities or IdentityCache()
    links = links or ExportLinks()
    solve_slots = SolveSlots(config.max_concurrent_solves)

    def source_for(site: Site) -> BoardSource:
        caller = current_caller()
        return KnownAccountSource(
            caller.account_id, source_factory(site, caller.credentials, settings)
        )

    def identify(credentials: Credentials) -> AccountId:
        return source_factory(config.site, credentials, settings).account_id()

    service = PlanService(
        settings=settings,
        registry=registry,
        store=store,
        source_for=source_for,
        scheduler=scheduler or PyJobShopScheduler(),
        rules=load_rules(),
        clock=clock,
        engine_version=__version__,
        owner=current_account,
        memoize=False,  # the store's TTL is a plan's lifetime; a memo would outlive it
        reuse_sources=False,
        stale_plans=False,
        solve_slots=solve_slots,
    )
    library_path = f"{STATIC_PATH}/{library_file_name()}"
    exports = ExportService(
        plans=service,
        store=store,
        reports=load_reports(),
        exporters=load_exporters(),
        files=MemoryFiles(limits),
        retention=timedelta(seconds=limits.ttl_s),
        clock=clock,
        owner=current_account,
        shared_library_url=f"{config.public_url}{library_path}",
    )

    def export_link(record: ExportRecord) -> str:
        return f"{config.public_url}{links.path(record.export_id, record.account_id)}"

    server = build_server(service, exports, export_link=export_link)
    server.add_middleware(CallerMiddleware(registry, config.site, identities, identify))

    @server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    @server.custom_route(library_path, methods=["GET"], include_in_schema=False)
    async def plotly_library(request: Request) -> Response:
        return Response(
            library_source(), media_type="text/javascript; charset=utf-8", headers=_STATIC_HEADERS
        )

    @server.custom_route(f"{EXPORTS_PATH}/{{token}}", methods=["GET"], include_in_schema=False)
    async def download(request: Request) -> Response:
        verified = links.verify(request.path_params["token"])
        if verified is not None:
            export_id, tag = verified
            try:
                record, content = exports.read_if(
                    ExportId(export_id), lambda account: links.owned_by(tag, account)
                )
            except PlanExpiredError:
                pass
            else:
                name = f"{record.export_id}.{record.format}"
                return Response(
                    content,
                    media_type=record.media_type,
                    headers={
                        **_DOWNLOAD_HEADERS,
                        "Content-Disposition": f'attachment; filename="{name}"',
                    },
                )
        return JSONResponse(
            {"error": "not_found", "message": "This download link is invalid or has expired."},
            status_code=404,
            headers=_DOWNLOAD_HEADERS,
        )

    return HostedApp(
        config=config,
        settings=settings,
        store=store,
        service=service,
        exports=exports,
        links=links,
        solve_slots=solve_slots,
        server=server,
    )


def serve(settings: Settings, console: Console, environ: Mapping[str, str] = os.environ) -> int:
    """``pulseboard serve`` with ``PULSEBOARD_TRANSPORT=http``; 2 on invalid configuration."""
    try:
        config = load_hosted_config(environ)
    except ConfigError as exc:
        console.warn(exc.message)
        return 2
    for name in credential_variables(environ):
        console.warn(
            f"{name} is set but ignored in HTTP mode: credentials come from each request's "
            "headers. Unset it (cf unset-env) and restage."
        )
    if settings.sites:
        console.warn("Sites in the settings file are ignored in HTTP mode (PULSEBOARD_SITE_URL).")
    configure_logging()
    create_hosted_app(settings, config).run()
    return 0
