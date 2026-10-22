"""Stub Jira for the image smoke test: replays recorded responses over HTTPS.

Runs inside a plain ``python`` container (standard library only), reachable from the Pulseboard
container under the recorded host name. Responses are matched by method, path, ``startAt``,
``expand`` and ``nextPageToken``; other query parameters (the JQL, page sizes) are ignored.

Usage: python smoke_stub_jira.py ROUTES_JSON CERT_PEM KEY_PEM [PORT]
"""

import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

Key = tuple[str, str, str, str, str]
MATCHED_PARAMETERS = ("startAt", "expand", "nextPageToken")


def route_key(method: str, target: str) -> Key:
    """The match key of a request: method, path and the matched parameters (empty when absent)."""
    parts = urlsplit(target)
    query = parse_qs(parts.query)
    start, expand, token = (query.get(name, [""])[0] for name in MATCHED_PARAMETERS)
    return method.upper(), parts.path, start, expand, token


def load_routes(path: Path) -> dict[Key, tuple[int, str]]:
    """Routes written by ``image_smoke.py``: a list of method, uri, status and body."""
    routes: dict[Key, tuple[int, str]] = {}
    for entry in json.loads(path.read_text(encoding="utf-8")):
        routes.setdefault(
            route_key(entry["method"], entry["uri"]), (entry["status"], entry["body"])
        )
    return routes


def handler_for(routes: dict[Key, tuple[int, str]]) -> type[BaseHTTPRequestHandler]:
    """A request handler serving ``routes``; unmatched requests get 404 and are logged."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            key = route_key(self.command, self.path)
            status, body = routes.get(key, (404, '{"errorMessages": ["not recorded"]}'))
            if status == 404:
                sys.stderr.write(f"unmatched {key}\n")
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json;charset=UTF-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self._reply()

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._reply()

        def log_message(self, format: str, *args: object) -> None:
            # Request lines only; headers (which carry the fake credentials) are never logged.
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path}\n")

    return Handler


def main(argv: list[str]) -> int:
    """Serve until stopped."""
    routes_json, cert, key = (Path(arg) for arg in argv[1:4])
    port = int(argv[4]) if len(argv) > 4 else 443
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(load_routes(routes_json)))  # noqa: S104 - container network only
    server.socket = context.wrap_socket(server.socket, server_side=True)
    sys.stderr.write(f"stub Jira listening on {port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
