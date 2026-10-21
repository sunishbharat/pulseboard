"""Profile A test helpers: the real ASGI app in-process (no sockets), fake Jira users."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.http import StarletteWithLifespan

from factories import fixture_board
from fakes import FakeSource
from pulseboard.credentials import Credentials
from pulseboard.domain.types import AccountId
from pulseboard.errors import AuthFailedError
from pulseboard.hosted.server import HostedApp
from pulseboard.settings import Settings
from pulseboard.sites import Site
from pulseboard.sources.base import BoardSource

BASE = "http://pulseboard.test"
PUBLIC_URL = "https://pulseboard.apps.example"
ACCESS_KEY = "team-a-access-key-0123"
OTHER_KEY = "team-b-access-key-4567"
ENVIRON = {
    "PULSEBOARD_TRANSPORT": "http",
    "PULSEBOARD_SITE_URL": "https://acme.atlassian.net",
    "PULSEBOARD_SITE_DEPLOYMENT": "cloud",
    "PULSEBOARD_ACCESS_KEYS": f"{ACCESS_KEY}, {OTHER_KEY}",
    "PULSEBOARD_PUBLIC_URL": PUBLIC_URL,
}
ACCOUNTS = {"alice-token": AccountId("acc-alice"), "bob-token": AccountId("acc-bob")}


def user(name: str, *, access_key: str = ACCESS_KEY) -> dict[str, str]:
    """Request headers of a fake Jira Cloud user."""
    return {
        "X-Pulseboard-Access-Key": access_key,
        "X-Jira-User-Email": f"{name}@example.com",
        "X-Jira-Api-Token": f"{name}-token",
    }


ALICE = user("alice")
BOB = user("bob")


class RejectingSource(FakeSource):
    """Jira answers 401 to the current-user call."""

    def account_id(self) -> AccountId:
        msg = "Jira rejected the credentials."
        raise AuthFailedError(msg)


@dataclass
class FakeJira:
    """A source factory per user; records every credential it was handed."""

    created: list[Credentials] = field(default_factory=list)
    sources: list[FakeSource] = field(default_factory=list)
    unavailable: bool = False

    def __call__(self, site: Site, credentials: Credentials, settings: Settings) -> BoardSource:
        self.created.append(credentials)
        token = credentials.token.get_secret_value() if credentials.token else ""
        account = ACCOUNTS.get(token)
        board = fixture_board()
        source = (
            FakeSource(
                [board.model_copy(update={"account_id": account})],
                unavailable=self.unavailable,
                _account=account,
            )
            if account
            else RejectingSource([board])
        )
        self.sources.append(source)
        return source

    @property
    def fetches(self) -> int:
        return sum(len(s.calls) for s in self.sources)


@asynccontextmanager
async def running(app: HostedApp) -> AsyncIterator[StarletteWithLifespan]:
    """The ASGI app with its lifespan (the MCP session manager) started."""
    asgi = app.http_app()
    async with asgi.router.lifespan_context(asgi):
        yield asgi


def http(asgi: StarletteWithLifespan) -> httpx2.AsyncClient:
    """A plain HTTP client talking to the app in-process."""
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi), base_url=BASE)


def mcp_client(asgi: StarletteWithLifespan, headers: dict[str, str]) -> Client[Any]:
    """An MCP client over streamable HTTP, sending ``headers`` with every request."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        *,
        follow_redirects: bool = True,  # passed by FastMCP, though not in the SDK protocol
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi),
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    transport = StreamableHttpTransport(
        f"{BASE}/mcp", headers=headers, httpx_client_factory=factory
    )
    return Client(transport)
