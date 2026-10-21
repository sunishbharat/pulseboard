"""Log redaction of credential headers and values (ADDENDUM-01 §1A.3)."""

import io
import logging
import socket
from collections.abc import Iterator

import anyio
import httpx2
import pytest
import uvicorn
from uvicorn.logging import AccessFormatter

from fakes import LINK
from hosted_harness import ALICE, http, mcp_client, running
from pulseboard.hosted.logs import (
    REDACTED,
    RedactingFilter,
    install_redaction,
    is_sensitive,
    redact,
    redact_text,
)
from pulseboard.hosted.server import HostedApp

SECRET = "alice-token"  # noqa: S105 - the fake token of hosted_harness.ALICE
STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class ExtrasFormatter(logging.Formatter):
    """The message, the exception and every ``extra`` field, so tests see all of it."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in vars(record).items() if k not in STANDARD}
        return f"{super().format(record)} {extras!r}"


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """A handler on the root logger, as configured in HTTP mode, with redaction installed."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ExtrasFormatter("%(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    level = root.level
    root.setLevel(logging.DEBUG)
    factory = logging.getLogRecordFactory()
    install_redaction()
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        logging.setLogRecordFactory(factory)


@pytest.fixture(autouse=True)
def _restore_record_factory() -> Iterator[None]:
    factory = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(factory)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "x-jira-api-token",
        "X-Jira-User-Email",
        "x-jira-pat",
        "X-Pulseboard-Access-Key",
        "PULSEBOARD_ACME_TOKEN",
        "PULSEBOARD_CORP_PAT",
        "api_key",
        "apiKey",
        "password",
        "client_secret",
        "client_cert",
        "PULSEBOARD_SITE_CLIENT_KEY_B64",
        "private_key",
        "cookie",
    ],
)
def test_sensitive_names(name: str) -> None:
    assert is_sensitive(name)


@pytest.mark.parametrize("name", ["path", "patch", "board_id", "status", "x-request-id", "host"])
def test_ordinary_names(name: str) -> None:
    assert not is_sensitive(name)


def test_nested_mappings_are_redacted() -> None:
    value = {"headers": {"x-jira-api-token": SECRET, "accept": "json"}, "items": [{"pat": "p"}]}

    assert redact(value) == {
        "headers": {"x-jira-api-token": REDACTED, "accept": "json"},
        "items": [{"pat": REDACTED}],
    }


@pytest.mark.parametrize(
    "text",
    [
        f"X-Jira-Api-Token: {SECRET}",
        f"headers={{'x-jira-api-token': '{SECRET}'}}",
        f'"x-jira-api-token": "{SECRET}"',
        f"Authorization: Basic {SECRET}",
        f"authorization=Bearer {SECRET}",
        f"GET /exports/{SECRET}.sig HTTP/1.1",
        f"token={SECRET}&board=42",
        f"x-pulseboard-access-key={SECRET}",
    ],
)
def test_secrets_in_text_are_masked(text: str) -> None:
    assert SECRET not in redact_text(text)


def test_ordinary_text_is_kept() -> None:
    text = "POST /mcp HTTP/1.1 200 path=/jira/software board=42"

    assert redact_text(text) == text


def test_filter_covers_args_extras_and_exceptions(captured: io.StringIO) -> None:
    logger = logging.getLogger("pulseboard.test")
    logger.info(
        "request %s", {"X-Jira-Api-Token": SECRET}, extra={"headers": {"x-jira-pat": SECRET}}
    )
    logger.info("%(x-jira-api-token)s", {"x-jira-api-token": SECRET})
    logger.warning("token=%s", SECRET, extra={"api_token": SECRET})
    try:
        raise ValueError(f"Authorization: Bearer {SECRET}")  # noqa: TRY003, TRY301 - logged
    except ValueError:
        logger.exception("failed")

    output = captured.getvalue()
    assert SECRET not in output
    assert output.count(REDACTED) >= 4


def test_install_is_idempotent() -> None:
    handler = logging.StreamHandler(io.StringIO())
    logging.getLogger("pulseboard.idempotent").addHandler(handler)
    try:
        install_redaction()
        install_redaction()
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        logging.getLogger("pulseboard.idempotent").removeHandler(handler)


async def test_a_full_request_never_logs_the_token(
    hosted: HostedApp, captured: io.StringIO
) -> None:
    async with running(hosted) as asgi:
        async with mcp_client(asgi, ALICE) as client:
            await client.call_tool("pb_build_plan", {"board_link": LINK})
            await client.call_tool("pb_plan_summary", {"plan_id": "0" * 32}, raise_on_error=False)
        async with http(asgi) as browser:
            await browser.get(f"/exports/{SECRET}")

    assert SECRET not in captured.getvalue()


# ---------------------------------------------------------------- signed export paths

ACCESS_FORMAT = '%(client_addr)s - "%(request_line)s" %(status_code)s'


def test_uvicorn_access_format_still_works_and_hides_the_token() -> None:
    install_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)  # added after start: only the record factory runs
    handler.setFormatter(AccessFormatter(ACCESS_FORMAT, use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    level, logger.level = logger.level, logging.INFO
    logger.addHandler(handler)
    try:
        # Exactly how uvicorn's protocols log a request.
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", f"/exports/{SECRET}.x", "1.1", 200
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)

    assert stream.getvalue() == f'127.0.0.1:5000 - "GET /exports/{REDACTED} HTTP/1.1" 200 OK\n'


def test_full_urls_and_messages_hide_export_tokens(captured: io.StringIO) -> None:
    logger = logging.getLogger("pulseboard.test")
    logger.info("link %s", f"https://pulseboard.apps.example/exports/{SECRET}")
    logger.info(f"served /exports/{SECRET} to the browser")  # noqa: G004 - prebuilt text
    logger.info("%(url)s", {"url": f"/exports/{SECRET}"})

    assert SECRET not in captured.getvalue()
    assert captured.getvalue().count(f"/exports/{REDACTED}") == 3


async def test_real_uvicorn_access_log_hides_the_token(
    hosted: HostedApp, captured: io.StringIO
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(hosted.http_app(), log_config=None, access_log=True, log_level="info")
    )
    async with anyio.create_task_group() as group:
        group.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0.02)
        async with httpx2.AsyncClient(trust_env=False) as client:
            missing = await client.get(f"http://127.0.0.1:{port}/exports/{SECRET}")
            health = await client.get(f"http://127.0.0.1:{port}/health")
        server.should_exit = True

    output = captured.getvalue()
    assert (missing.status_code, health.status_code) == (404, 200)
    assert f'"GET /exports/{REDACTED} HTTP/1.1" 404' in output  # the access line is still there
    assert '"GET /health HTTP/1.1" 200' in output
    assert SECRET not in output
