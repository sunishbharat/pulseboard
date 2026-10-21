"""Shared fixtures. Unit tests may not touch the network: any non-loopback connection fails."""

import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import SecretStr

from factories import fixture_board
from fakes import ENV, Clock, FakeSource
from hosted_harness import ENVIRON, FakeJira
from network_guard import NetworkBlockedError, NetworkGuard
from pulseboard.app import App, create_app, open_store
from pulseboard.credentials import Credentials
from pulseboard.domain.types import AuthMethod, Deployment, RequestProfile, SiteId
from pulseboard.hosted.config import HostedConfig, load_hosted_config
from pulseboard.hosted.server import HostedApp, create_hosted_app
from pulseboard.settings import Settings
from pulseboard.sites import Site
from pulseboard.sources.base import BoardSource

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(autouse=True)
def network_guard(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[NetworkGuard]:
    """Block DNS lookups and non-loopback connections (loopback stays open for event loops)."""
    guard = NetworkGuard()
    if request.node.get_closest_marker("integration"):
        yield guard
        return

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK:
            real_connect(sock, address)  # type: ignore[arg-type]
            return
        guard.attempts.append(f"connect {host}")
        msg = f"network access blocked in unit tests: {host}"
        raise NetworkBlockedError(msg)

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host in _LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]
        guard.attempts.append(f"resolve {host}")
        msg = f"DNS lookup blocked in unit tests: {host}"
        raise NetworkBlockedError(msg)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield guard


@pytest.fixture
def cloud_site() -> Site:
    """A Jira Cloud site."""
    return Site(
        site_id=SiteId("acme"),
        base_url="https://acme.atlassian.net",
        deployment=Deployment.CLOUD,
        auth_method=AuthMethod.API_TOKEN,
    )


@pytest.fixture
def dc_site() -> Site:
    """A Jira Data Center site with a context path and a custom port."""
    return Site(
        site_id=SiteId("corp"),
        base_url="https://jira.corp.example:8443/jira/",
        deployment=Deployment.DATA_CENTER,
        auth_method=AuthMethod.PAT,
    )


@pytest.fixture
def asf_site() -> Site:
    """A public Data Center site read anonymously under the polite profile."""
    return Site(
        site_id=SiteId("asf"),
        base_url="https://issues.example.org/jira",
        deployment=Deployment.DATA_CENTER,
        auth_method=AuthMethod.ANONYMOUS,
        profile=RequestProfile.POLITE,
        contact="https://example.org/pulseboard-contact",
    )


# ---------------------------------------------------------------- source adapters

CASSETTES = Path(__file__).parent / "fixtures" / "cassettes"


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    """Replay only, match on the full request, never store credentials."""
    return {
        "record_mode": "none",
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "filter_headers": ["authorization"],
    }


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Hand-built cassettes shared by all source tests."""
    return str(CASSETTES)


@pytest.fixture
def cloud_creds() -> Credentials:
    """Cloud API token credentials (fake)."""
    return Credentials(email="alice@example.com", token=SecretStr("cloud-token"))


@pytest.fixture
def dc_creds() -> Credentials:
    """Data Center personal access token (fake)."""
    return Credentials(email=None, token=SecretStr("dc-pat"))


# ---------------------------------------------------------------- MCP server


@pytest.fixture
def source() -> FakeSource:
    return FakeSource([fixture_board()])


@pytest.fixture
def clock() -> Clock:
    """The app's clock: 2026-10-19 08:00 UTC (the fixture board's plan date)."""
    fixed = Clock()
    fixed.now = datetime(2026, 10, 19, 8, tzinfo=UTC)
    return fixed


@pytest.fixture
def app(cloud_site: Site, tmp_path: Path, source: FakeSource, clock: Clock) -> App:
    settings = Settings(sites=(cloud_site,), data_dir=tmp_path)

    def factory(site: Site, creds: Credentials, settings: Settings) -> BoardSource:
        return source

    return create_app(
        settings,
        store=open_store(settings, "sqlite://"),
        source_factory=factory,
        clock=clock,
        environ=ENV,
        keychain=None,
    )


@pytest.fixture
async def client(app: App) -> AsyncIterator[Client[FastMCPTransport]]:
    async with Client(app.server) as c:
        yield c


# ---------------------------------------------------------------- hosted Profile A


@pytest.fixture
def config() -> HostedConfig:
    """Profile A settings for a Cloud site with two access keys."""
    return load_hosted_config(ENVIRON)


@pytest.fixture
def jira() -> FakeJira:
    """Fake Jira users ``alice`` and ``bob`` (see ``hosted_harness``)."""
    return FakeJira()


@pytest.fixture
def hosted(config: HostedConfig, jira: FakeJira, clock: Clock) -> HostedApp:
    """The Profile A app on fake Jira users."""
    return create_hosted_app(Settings(), config, source_factory=jira, clock=clock)
