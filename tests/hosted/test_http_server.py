"""Profile A over HTTP (ADDENDUM-01 §1A, ADR-022 to ADR-025), with no network: the ASGI app runs
in-process and Jira is a fake per user."""

import sys
import threading
from typing import Any

import anyio
import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from fakes import LINK, Clock
from hosted_harness import (
    ACCESS_KEY,
    ALICE,
    BOB,
    ENVIRON,
    OTHER_KEY,
    PUBLIC_URL,
    FakeJira,
    http,
    mcp_client,
    running,
)
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.scheduling import ScheduleRequest, ScheduleResult
from pulseboard.exports.formats import library_file_name, library_source
from pulseboard.hosted.config import HostedConfig, load_hosted_config
from pulseboard.hosted.memory import MemoryLimits
from pulseboard.hosted.server import MCP_PATH, HostedApp, create_hosted_app
from pulseboard.settings import Limits, Settings

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
FOREIGN_LINK = "https://evil.example.com/jira/software/projects/ABC/boards/42"


async def call(client: Client[Any], name: str, **args: object) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    assert result.structured_content is not None
    return result.structured_content


async def error(client: Client[Any], name: str, **args: object) -> str:
    result = await client.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    return " ".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------- routes and access key


async def test_health_needs_no_access_key(hosted: HostedApp) -> None:
    async with running(hosted) as asgi, http(asgi) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("key", [None, "", "wrong-key-0123456789", ACCESS_KEY[:-1]])
async def test_mcp_requires_a_valid_access_key(hosted: HostedApp, key: str | None) -> None:
    headers = {**MCP_HEADERS, **{k: v for k, v in ALICE.items() if "Access" not in k}}
    if key is not None:
        headers["X-Pulseboard-Access-Key"] = key
    async with running(hosted) as asgi, http(asgi) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"] == "access_denied"
    assert "alice-token" not in response.text


@pytest.mark.parametrize("key", [ACCESS_KEY, OTHER_KEY])
async def test_every_configured_access_key_is_accepted(hosted: HostedApp, key: str) -> None:
    headers = {**MCP_HEADERS, "X-Pulseboard-Access-Key": key}
    async with running(hosted) as asgi, http(asgi) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers=headers)

    assert response.status_code == 200


async def test_stateless_http_needs_no_session(hosted: HostedApp) -> None:
    async with running(hosted) as asgi, http(asgi) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers={**MCP_HEADERS, **ALICE})

    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers


# ---------------------------------------------------------------- credential headers


@pytest.mark.parametrize(
    "dropped",
    [
        ("X-Jira-User-Email", "X-Jira-Api-Token"),  # none
        ("X-Jira-User-Email",),  # half: token only
        ("X-Jira-Api-Token",),  # half: email only
    ],
)
async def test_missing_or_half_credentials_are_a_hard_error(
    hosted: HostedApp, jira: FakeJira, dropped: tuple[str, ...]
) -> None:
    headers = {k: v for k, v in ALICE.items() if k not in dropped}
    async with running(hosted) as asgi, mcp_client(asgi, headers) as client:
        text = await error(client, "pb_build_plan", board_link=LINK)

    assert text.startswith("auth_failed: missing per-user Jira credentials")
    assert jira.created == []  # no fallback identity, no Jira source at all


async def test_empty_credential_header_counts_as_missing(hosted: HostedApp, jira: FakeJira) -> None:
    async with running(hosted) as asgi, mcp_client(asgi, {**ALICE, "X-Jira-Api-Token": " "}) as c:
        text = await error(c, "pb_plan_summary", plan_id="0" * 32)

    assert text.startswith("auth_failed: missing per-user Jira credentials")
    assert jira.created == []


async def test_credentials_from_environment_are_never_used(
    hosted: HostedApp, jira: FakeJira, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSEBOARD_JIRA_EMAIL", "service@example.com")
    monkeypatch.setenv("PULSEBOARD_JIRA_TOKEN", "alice-token")
    headers = {"X-Pulseboard-Access-Key": ACCESS_KEY}
    async with running(hosted) as asgi, mcp_client(asgi, headers) as client:
        text = await error(client, "pb_build_plan", board_link=LINK)

    assert text.startswith("auth_failed: missing per-user Jira credentials")
    assert jira.created == []


async def test_rejected_token_is_auth_failed(hosted: HostedApp) -> None:
    headers = {**ALICE, "X-Jira-Api-Token": "revoked-token"}
    async with running(hosted) as asgi, mcp_client(asgi, headers) as client:
        text = await error(client, "pb_build_plan", board_link=LINK)

    assert text.startswith("auth_failed: Jira rejected the credentials.")


async def test_list_sites_needs_only_the_access_key(hosted: HostedApp, jira: FakeJira) -> None:
    headers = {"X-Pulseboard-Access-Key": ACCESS_KEY}
    async with running(hosted) as asgi, mcp_client(asgi, headers) as client:
        sites = await call(client, "pb_list_sites")

    assert sites["sites"] == [
        {"site_id": "jira", "base_url": "https://acme.atlassian.net", "deployment": "cloud"}
    ]
    assert jira.created == []


# ---------------------------------------------------------------- one site (ADR-024)


@pytest.mark.parametrize("tool", ["pb_build_plan", "pb_flow_metrics"])
async def test_foreign_board_host_is_rejected_before_any_call(
    hosted: HostedApp, jira: FakeJira, tool: str
) -> None:
    async with running(hosted) as asgi, mcp_client(asgi, ALICE) as client:
        text = await error(client, tool, board_link=FOREIGN_LINK)

    assert text.startswith("site_not_configured:")
    assert jira.created == []  # the caller's token never reached any source, not even /myself


# ---------------------------------------------------------------- per-user caches


async def test_plan_round_trip_for_one_user(hosted: HostedApp, jira: FakeJira) -> None:
    async with running(hosted) as asgi, mcp_client(asgi, ALICE) as client:
        built = await call(client, "pb_build_plan", board_link=LINK)
        summary = await call(client, "pb_plan_summary", plan_id=built["plan_id"])

    assert built["plan_id"]
    assert summary["plan_id"] == built["plan_id"]
    assert jira.fetches == 1
    assert [c.email for c in jira.created] == ["alice@example.com"] * len(jira.created)


async def test_two_users_never_see_each_others_plans(hosted: HostedApp, jira: FakeJira) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            alice_plan = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
        async with mcp_client(asgi, BOB) as bob:
            stolen = await error(bob, "pb_plan_summary", plan_id=alice_plan)
            with pytest.raises(MCPError, match="^plan_expired:"):
                await bob.read_resource(f"pulseboard://plans/{alice_plan}")
            bob_plan = (await call(bob, "pb_build_plan", board_link=LINK))["plan_id"]

    assert stolen.startswith("plan_expired:")
    assert bob_plan != alice_plan
    assert jira.fetches == 2  # Bob's board was fetched with Bob's token, not served from Alice's
    assert hosted.store.get_plan(alice_plan).account_id == "acc-alice"  # type: ignore[union-attr]


async def test_two_users_never_see_each_others_exports(hosted: HostedApp) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            plan = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
            rendered = await call(alice, "pb_render", plan_id=plan, report_id="gantt")
            own = await alice.read_resource(rendered["uri"])
        async with mcp_client(asgi, BOB) as bob:
            with pytest.raises(MCPError, match="^plan_expired: This export is unknown"):
                await bob.read_resource(rendered["uri"])

    assert own


async def test_resource_read_without_credentials_is_refused(hosted: HostedApp) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            plan = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
        async with mcp_client(asgi, {"X-Pulseboard-Access-Key": ACCESS_KEY}) as anonymous:
            with pytest.raises(MCPError, match="^auth_failed: missing per-user Jira credentials"):
                await anonymous.read_resource(f"pulseboard://plans/{plan}")


# ---------------------------------------------------------------- signed download links


async def test_download_link_serves_the_export_once_signed(hosted: HostedApp) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            plan = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
            rendered = await call(
                alice, "pb_render", plan_id=plan, report_id="throughput", format="csv"
            )
        prefix, token = rendered["download_url"].rsplit("/", 1)
        forged = f"{prefix}/{'X' if token[0] != 'X' else 'Y'}{token[1:]}"
        async with http(asgi) as browser:  # no access key, no Jira headers: a browser click
            ok = await browser.get(rendered["download_url"])
            tampered = await browser.get(forged)

    assert rendered["download_url"].startswith(f"{PUBLIC_URL}/exports/")
    assert ok.status_code == 200
    assert ok.text.startswith("week_start,done")
    assert ok.headers["content-disposition"].startswith("attachment;")
    assert ok.headers["cache-control"] == "no-store"
    assert tampered.status_code == 404


async def test_download_link_is_bound_to_its_owner(hosted: HostedApp) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            plan = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
            rendered = await call(alice, "pb_render", plan_id=plan, report_id="gantt")
        async with mcp_client(asgi, BOB) as bob:
            bob_plan = (await call(bob, "pb_build_plan", board_link=LINK))["plan_id"]
            bobs = await call(bob, "pb_render", plan_id=bob_plan, report_id="gantt")
        alice_export = rendered["uri"].rsplit("/", 1)[1]
        bob_export = bobs["uri"].rsplit("/", 1)[1]
        # A valid signature for Bob's export, claiming Alice's export ID, and the reverse.
        forged = hosted.links.path(alice_export, "acc-bob")  # type: ignore[arg-type]
        swapped = hosted.links.path(bob_export, "acc-alice")  # type: ignore[arg-type]
        async with http(asgi) as browser:
            responses = [await browser.get(path) for path in (forged, swapped)]

    assert [r.status_code for r in responses] == [404, 404]


async def test_pdf_export_is_refused_with_a_clear_message(hosted: HostedApp) -> None:
    async with running(hosted) as asgi, mcp_client(asgi, ALICE) as client:
        plan = (await call(client, "pb_build_plan", board_link=LINK))["plan_id"]
        text = await error(client, "pb_render", plan_id=plan, report_id="gantt", format="pdf")
        still_works = await call(client, "pb_render", plan_id=plan, report_id="gantt")

    assert text == (
        "not_available: PDF export is not available on this server."
        " Hint: Use html, xlsx, csv or json."
    )
    assert still_works["download_url"].startswith(f"{PUBLIC_URL}/exports/")
    assert "weasyprint" not in sys.modules


async def test_links_never_come_from_request_host_or_scheme_headers(hosted: HostedApp) -> None:
    spoofed = {
        **ALICE,
        "Host": "evil.example",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "http",
        "Forwarded": "host=evil.example;proto=http",
    }
    async with running(hosted) as asgi, mcp_client(asgi, spoofed) as client:
        plan = (await call(client, "pb_build_plan", board_link=LINK))["plan_id"]
        rendered = await call(client, "pb_render", plan_id=plan, report_id="gantt")

    assert rendered["download_url"].startswith(f"{PUBLIC_URL}/exports/")
    assert "evil" not in rendered["download_url"]


# ---------------------------------------------------------------- shared Plotly library


async def test_shared_mode_loads_plotly_from_the_static_path(hosted: HostedApp) -> None:
    library_url = f"{PUBLIC_URL}/static/{library_file_name()}"
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as client:
            plan = (await call(client, "pb_build_plan", board_link=LINK))["plan_id"]
            inline = await call(client, "pb_render", plan_id=plan, report_id="gantt")
            shared = await call(
                client, "pb_render", plan_id=plan, report_id="gantt", include_plotlyjs="shared"
            )
            page = (await client.read_resource(shared["uri"]))[0]
        async with http(asgi) as browser:  # no access key: the browser loads it from the page
            script = await browser.get(library_url)

    html = getattr(page, "text", "")
    assert f'src="{library_url}"' in html
    assert shared["size_bytes"] < inline["size_bytes"] // 4  # no embedded library
    assert script.status_code == 200
    assert script.text == library_source()
    assert script.headers["content-type"].startswith("text/javascript")
    assert "immutable" in script.headers["cache-control"]


async def test_only_the_installed_library_is_static(hosted: HostedApp) -> None:
    async with running(hosted) as asgi, http(asgi) as browser:
        other = await browser.get("/static/plotly-0.0.0.min.js")
        listing = await browser.get("/static/")

    assert other.status_code == 404
    assert listing.status_code == 404


# ---------------------------------------------------------------- solve concurrency


class GatedScheduler:
    """The real scheduler, held at a gate so a solve can outlast the tool timeout."""

    def __init__(self) -> None:
        self.inner = PyJobShopScheduler()
        self.gate = threading.Event()
        self.solves = 0

    def solve(self, request: ScheduleRequest) -> ScheduleResult:
        self.solves += 1
        self.gate.wait(30)
        return self.inner.solve(request)


async def settled(hosted: HostedApp) -> None:
    """Wait until every solver slot is free again (abandoned solves finish in the background)."""
    with anyio.fail_after(60):
        while hosted.solve_slots.in_use:
            await anyio.sleep(0.05)


async def test_excess_solves_wait_for_the_timeout_then_get_server_busy(
    config: HostedConfig, jira: FakeJira, clock: Clock
) -> None:
    scheduler = GatedScheduler()
    settings = Settings(limits=Limits(tool_timeout_s=0.5))
    hosted = create_hosted_app(
        settings, config, source_factory=jira, clock=clock, scheduler=scheduler
    )
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as alice:
            timed_out = await call(alice, "pb_build_plan", board_link=LINK)
        # Alice's solve was abandoned at her timeout but still runs, so it keeps the only slot.
        assert hosted.solve_slots.in_use == 1
        async with mcp_client(asgi, BOB) as bob:
            started = anyio.current_time()
            busy = await error(bob, "pb_build_plan", board_link=LINK)
            waited = anyio.current_time() - started
        scheduler.gate.set()
        await settled(hosted)
        async with mcp_client(asgi, BOB) as bob:
            built = await call(bob, "pb_build_plan", board_link=LINK)

    assert timed_out["plan_id"] is None  # a solve that ran too long: partial result, as before
    assert busy == "server_busy: The server is busy planning other boards. Hint: Retry shortly."
    assert waited >= 0.5
    assert scheduler.solves == 2  # Bob's first request never reached the solver
    assert built["plan_id"]


async def test_concurrent_requests_share_the_configured_slots(jira: FakeJira, clock: Clock) -> None:
    config = load_hosted_config({**ENVIRON, "PULSEBOARD_MAX_CONCURRENT_SOLVES": "2"})
    scheduler = GatedScheduler()
    settings = Settings(limits=Limits(tool_timeout_s=0.5))
    hosted = create_hosted_app(
        settings, config, source_factory=jira, clock=clock, scheduler=scheduler
    )
    results: dict[str, dict[str, Any]] = {}

    async with running(hosted) as asgi:

        async def build(name: str, headers: dict[str, str]) -> None:
            async with mcp_client(asgi, headers) as client:
                results[name] = await call(client, "pb_build_plan", board_link=LINK)

        async with anyio.create_task_group() as group:
            group.start_soon(build, "alice", ALICE)
            group.start_soon(build, "bob", BOB)
        scheduler.gate.set()
        await settled(hosted)

    # Both solves started (two slots) and ran past the timeout: partial results, nobody busy.
    assert scheduler.solves == 2
    assert results["alice"]["plan_id"] is None
    assert results["bob"]["plan_id"] is None


async def test_jira_outage_says_stale_plans_need_durable_storage(
    hosted: HostedApp, jira: FakeJira
) -> None:
    jira.unavailable = True
    async with running(hosted) as asgi, mcp_client(asgi, ALICE) as client:
        text = await error(client, "pb_build_plan", board_link=LINK)

    assert text.startswith("jira_unavailable: Jira is unreachable.")
    assert "requires durable storage" in text


async def test_a_plan_expires_with_the_memory_lifetime(jira: FakeJira, clock: Clock) -> None:
    """PB-05: no cache outlives the store's TTL, so an hour-old plan_id is plan_expired."""
    hosted = create_hosted_app(
        Settings(),
        load_hosted_config(ENVIRON),
        source_factory=jira,
        clock=clock,
        limits=MemoryLimits(ttl_s=3.0),
    )
    async with running(hosted) as asgi, mcp_client(asgi, ALICE) as alice:
        plan_id = (await call(alice, "pb_build_plan", board_link=LINK))["plan_id"]
        assert (await call(alice, "pb_plan_summary", plan_id=plan_id))["plan_id"] == plan_id
        await anyio.sleep(3.5)  # past the memory lifetime (the store's monotonic timer)
        expired = await error(alice, "pb_milestones", plan_id=plan_id)  # reads the plan only

    assert expired.startswith("plan_expired:")
