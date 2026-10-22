"""Smoke test of a built Profile A image (ADDENDUM-01 §1A.6, §16 M7.A2).

Starts the image the way Cloud Foundry does (dynamic ``PORT``, settings from environment
variables), next to a stub Jira that replays ``tests/fixtures/cassettes/cloud_board.yaml`` over
HTTPS under the recorded host name. Then checks, in order:

1. ``pulseboard --help`` runs, the image runs as uid 1000 and has neither matplotlib nor the
   packages only it needs (pillow among them: no PDF image)
2. ``/health`` answers
3. ``/mcp`` refuses a request without the endpoint access key
4. ``pb_build_plan`` returns a plan for the fixture board, using fake credential headers
5. the fake token never appears in the server's log

Needs Docker and ``openssl``. Usage:
    uv run python scripts/image_smoke.py --image pulseboard:dev
"""

import argparse
import asyncio
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import certifi
import yaml
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent.parent
CASSETTE = ROOT / "tests" / "fixtures" / "cassettes" / "cloud_board.yaml"
STUB = Path(__file__).resolve().parent / "smoke_stub_jira.py"
STUB_IMAGE = "python:3.12-slim-bookworm"
JIRA_HOST = "acme.atlassian.net"
BOARD_LINK = f"https://{JIRA_HOST}/jira/software/projects/ABC/boards/42"
CONTAINER_PORT = "8080"  # set as PORT, as Cloud Foundry does
ACCESS_KEY = "smoke-access-key"
FAKE_TOKEN = "smoke-fake-api-token-" + secrets.token_hex(8)  # noqa: S105 - not a real secret
FAKE_EMAIL = "smoke@example.com"
HEALTH_TIMEOUT_S = 90
# Import names of matplotlib and the packages only it needs (deploy/Dockerfile leaves them out).
LEFT_OUT = ("matplotlib", "PIL", "fontTools", "kiwisolver", "contourpy", "pyparsing", "cycler")
REJECTED = (401, 403)


class SmokeError(RuntimeError):
    """A smoke check failed."""


def docker(*args: str, check: bool = True) -> str:
    """Runs ``docker`` and returns its standard output."""
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        msg = f"docker {' '.join(args[:2])} failed: {done.stderr.strip()}"
        raise SmokeError(msg)
    return done.stdout.strip()


def stub_routes(recorded: list[dict[str, Any]]) -> list[dict[str, object]]:
    """The cassette's interactions as stub routes. The recorded changelog searches (pages of
    the tests' page size) become one route holding every issue, since the server's page size
    differs and the stub ignores the JQL."""
    routes: list[dict[str, object]] = []
    changelogs: list[object] = []
    for item in recorded:
        uri = item["request"]["uri"]
        body = item["response"]["body"]["string"]
        if "/search/jql" in uri and "expand=changelog" in uri:
            changelogs.extend(json.loads(body)["issues"])
            continue
        routes.append(
            {
                "method": item["request"]["method"],
                "uri": uri,
                "status": item["response"]["status"]["code"],
                "body": body,
            }
        )
    routes.append(
        {
            "method": "GET",
            "uri": f"https://{JIRA_HOST}/rest/api/3/search/jql?expand=changelog",
            "status": 200,
            "body": json.dumps({"issues": changelogs, "isLast": True}),
        }
    )
    return routes


def prepare(work: Path) -> None:
    """Writes the stub's certificate, the combined CA bundle, routes and script into ``work``."""
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(work / "key.pem"), "-out", str(work / "cert.pem"),
            "-subj", f"/CN={JIRA_HOST}", "-addext", f"subjectAltName=DNS:{JIRA_HOST}",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    # certifi plus the stub's certificate, never the stub's alone (ADDENDUM-01 L6).
    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    bundle += "\n" + (work / "cert.pem").read_text(encoding="utf-8")
    (work / "ca.pem").write_text(bundle, encoding="utf-8")

    recorded = yaml.safe_load(CASSETTE.read_text(encoding="utf-8"))["interactions"]
    (work / "routes.json").write_text(json.dumps(stub_routes(recorded)), encoding="utf-8")
    shutil.copy(STUB, work / STUB.name)
    # Readable by the containers' users (root in the stub, uid 1000 in Pulseboard).
    work.chmod(0o755)
    for path in work.iterdir():
        path.chmod(0o644)


def wait_for_health(base: str, app: str) -> None:
    """Polls ``/health`` until it answers 200, failing early if the container exits."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if docker("inspect", "-f", "{{.State.Running}}", app) != "true":
            msg = "the Pulseboard container exited before /health answered"
            raise SmokeError(msg)
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as response:  # noqa: S310 - fixed local URL
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    msg = f"/health did not answer within {HEALTH_TIMEOUT_S} s"
    raise SmokeError(msg)


def check_access_key_required(base: str) -> None:
    """An MCP request without ``X-Pulseboard-Access-Key`` must be refused (ADR-025)."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed local URL
        f"{base}/mcp",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Jira-User-Email": FAKE_EMAIL,
            "X-Jira-Api-Token": FAKE_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    if status not in REJECTED:
        msg = f"/mcp without an access key answered {status}, expected 401 or 403"
        raise SmokeError(msg)


async def build_plan(base: str) -> str:
    """Calls ``pb_build_plan`` with fake headers and returns the plan ID."""
    transport = StreamableHttpTransport(
        f"{base}/mcp",
        headers={
            "X-Pulseboard-Access-Key": ACCESS_KEY,
            "X-Jira-User-Email": FAKE_EMAIL,
            "X-Jira-Api-Token": FAKE_TOKEN,
        },
    )
    async with Client(transport, timeout=120) as client:
        result = await client.call_tool("pb_build_plan", {"board_link": BOARD_LINK})
    plan_id = (result.structured_content or {}).get("plan_id")
    if not plan_id:
        msg = f"pb_build_plan returned no plan_id: {result.structured_content!r}"
        raise SmokeError(msg)
    return str(plan_id)


def container_logs(name: str) -> str:
    """Standard output and error of a container (empty if it does not exist)."""
    done = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
    return done.stdout + done.stderr


def check_token_not_logged(app: str) -> None:
    """The caller's Jira token never reaches the log (ADDENDUM-01 §1A.3)."""
    if FAKE_TOKEN in container_logs(app):
        msg = "the Jira token appears in the server log"
        raise SmokeError(msg)


def run(image: str, host_port: int) -> None:
    """All checks against ``image``; containers and the network are always removed."""
    print(docker("run", "--rm", image, "pulseboard", "--help").splitlines()[0])
    uid = docker("run", "--rm", image, "id", "-u")
    if uid != "1000":
        msg = f"the image runs as uid {uid}, expected 1000"
        raise SmokeError(msg)
    present = docker(
        "run", "--rm", image, "python", "-c",
        "import importlib.util as u; "
        f"print(' '.join(m for m in {LEFT_OUT!r} if u.find_spec(m) is not None))",
    )  # fmt: skip
    if present:
        msg = f"packages left out of the image are installed: {present}"
        raise SmokeError(msg)

    suffix = secrets.token_hex(4)
    network, stub, app = (f"pb-smoke-{suffix}", f"pb-smoke-jira-{suffix}", f"pb-smoke-app-{suffix}")
    work = Path(tempfile.mkdtemp(prefix="pb-smoke-"))
    base = f"http://127.0.0.1:{host_port}"
    try:
        prepare(work)
        docker("network", "create", network)
        docker(
            "run", "-d", "--name", stub, "--network", network, "--network-alias", JIRA_HOST,
            "-v", f"{work}:/smoke:ro", STUB_IMAGE,
            "python", f"/smoke/{STUB.name}", "/smoke/routes.json", "/smoke/cert.pem",
            "/smoke/key.pem",
        )  # fmt: skip
        docker(
            "run", "-d", "--name", app, "--network", network,
            "-p", f"127.0.0.1:{host_port}:{CONTAINER_PORT}",
            "-e", f"PORT={CONTAINER_PORT}",
            "-e", f"PULSEBOARD_SITE_URL=https://{JIRA_HOST}",
            "-e", "PULSEBOARD_SITE_DEPLOYMENT=cloud",
            "-e", f"PULSEBOARD_ACCESS_KEYS={ACCESS_KEY}",
            "-e", f"PULSEBOARD_PUBLIC_URL=http://127.0.0.1:{host_port}",
            # Test-only trust for the stub; on the platform CA bundles are baked in (§5.3).
            "-e", "REQUESTS_CA_BUNDLE=/smoke/ca.pem",
            "-e", "SSL_CERT_FILE=/smoke/ca.pem",
            "-v", f"{work}:/smoke:ro",
            image,
        )  # fmt: skip
        wait_for_health(base, app)
        print("health: ok")
        check_access_key_required(base)
        print("access key required: ok")
        print(f"pb_build_plan: ok (plan {asyncio.run(build_plan(base))})")
        check_token_not_logged(app)
        print("token redacted from logs: ok")
    except BaseException:
        for name in (app, stub):
            sys.stderr.write(f"--- docker logs {name}\n{container_logs(name)}")
        raise
    finally:
        docker("rm", "-f", app, stub, check=False)
        docker("network", "rm", network, check=False)
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the smoke test; return 1 on failure."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="image reference to test")
    parser.add_argument("--port", type=int, default=18765, help="host port for the server")
    args = parser.parse_args(argv)
    try:
        run(args.image, args.port)
    except SmokeError as error:
        sys.stderr.write(f"smoke test failed: {error}\n")
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
