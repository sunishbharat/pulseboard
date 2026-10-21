"""Profile A configuration from the environment (ADDENDUM-01 §1A.5) and the CLI dispatch."""

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from fakes import ENV, FakeKeychain
from hosted_harness import ACCESS_KEY, ENVIRON, PUBLIC_URL
from pulseboard.cli import main
from pulseboard.cli.console import Console
from pulseboard.domain.types import AuthMethod, Deployment
from pulseboard.errors import ConfigError
from pulseboard.hosted.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Transport,
    credential_variables,
    load_hosted_config,
    transport_from,
)


def env(**changes: str | None) -> dict[str, str]:
    merged: dict[str, str | None] = {**ENVIRON, **changes}
    return {k: v for k, v in merged.items() if v is not None}


def test_cloud_site_from_environment() -> None:
    config = load_hosted_config(ENVIRON)

    assert config.site.base_url == "https://acme.atlassian.net"
    assert config.site.deployment is Deployment.CLOUD
    assert config.site.auth_method is AuthMethod.API_TOKEN
    assert [k.get_secret_value() for k in config.access_keys][0] == ACCESS_KEY
    assert len(config.access_keys) == 2
    assert (config.host, config.port) == (DEFAULT_HOST, DEFAULT_PORT)
    assert config.public_url == PUBLIC_URL
    assert config.max_concurrent_solves == 1
    assert ACCESS_KEY not in repr(config)


def test_data_center_site_uses_personal_access_tokens() -> None:
    config = load_hosted_config(
        env(
            PULSEBOARD_SITE_URL="https://jira.corp.example/jira",
            PULSEBOARD_SITE_DEPLOYMENT="datacenter",
        )
    )

    assert config.site.auth_method is AuthMethod.PAT


def test_port_host_and_public_url() -> None:
    config = load_hosted_config(
        env(
            PULSEBOARD_PORT="8080",
            PULSEBOARD_HOST="0.0.0.0",  # noqa: S104 - the container binds all interfaces
            PULSEBOARD_PUBLIC_URL="https://pulseboard.apps.internal/",
        )
    )

    assert (config.host, config.port) == ("0.0.0.0", 8080)  # noqa: S104
    assert config.public_url == "https://pulseboard.apps.internal"


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"PULSEBOARD_SITE_URL": None}, "PULSEBOARD_SITE_URL is required"),
        ({"PULSEBOARD_SITE_DEPLOYMENT": "server"}, "must be 'cloud' or 'datacenter'"),
        ({"PULSEBOARD_SITE_DEPLOYMENT": "datacenter"}, "Jira Cloud host"),
        ({"PULSEBOARD_ACCESS_KEYS": " , "}, "PULSEBOARD_ACCESS_KEYS is required"),
        ({"PULSEBOARD_ACCESS_KEYS": "short"}, "at least 16 characters"),
        ({"PULSEBOARD_PORT": "http"}, "must be a port number"),
        ({"PULSEBOARD_PORT": "70000"}, "must be a port number"),
        ({"PULSEBOARD_SITE_CLIENT_CERT": "true"}, "client certificates are not supported yet"),
        ({"PULSEBOARD_SITE_CLIENT_CERT": "maybe"}, "must be true or false"),
        ({"PULSEBOARD_PUBLIC_URL": None}, "PULSEBOARD_PUBLIC_URL is required"),
        ({"PULSEBOARD_PUBLIC_URL": " "}, "PULSEBOARD_PUBLIC_URL is required"),
        ({"PULSEBOARD_PUBLIC_URL": "pulseboard.internal"}, "must be an http(s) URL"),
        ({"PULSEBOARD_PUBLIC_URL": "ftp://pulseboard.internal"}, "must be an http(s) URL"),
        ({"PULSEBOARD_PUBLIC_URL": "https://"}, "must be an http(s) URL"),
        ({"PULSEBOARD_PUBLIC_URL": "https://u:p@pulseboard.internal"}, "no user, query"),
        ({"PULSEBOARD_PUBLIC_URL": "https://pulseboard.internal/?a=1"}, "no user, query"),
        ({"PULSEBOARD_PUBLIC_URL": "https://pulseboard.internal/#x"}, "no user, query"),
        ({"PULSEBOARD_MAX_CONCURRENT_SOLVES": "0"}, "must be 1 or 2"),
        ({"PULSEBOARD_MAX_CONCURRENT_SOLVES": "3"}, "must be 1 or 2"),
        ({"PULSEBOARD_MAX_CONCURRENT_SOLVES": "two"}, "must be 1 or 2"),
    ],
)
def test_invalid_settings_are_listed_without_values(changes: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_hosted_config(env(**changes))

    assert problem in caught.value.message
    assert ACCESS_KEY not in caught.value.message


def test_public_url_may_carry_a_path_prefix_and_two_solves() -> None:
    config = load_hosted_config(
        env(
            PULSEBOARD_PUBLIC_URL="https://apps.internal/pulseboard/",
            PULSEBOARD_MAX_CONCURRENT_SOLVES="2",
        )
    )

    assert config.public_url == "https://apps.internal/pulseboard"
    assert config.max_concurrent_solves == 2


def test_transport_defaults_to_stdio() -> None:
    assert transport_from({}) is Transport.STDIO
    assert transport_from({"PULSEBOARD_TRANSPORT": " HTTP "}) is Transport.HTTP
    with pytest.raises(ConfigError, match="'stdio' or 'http'"):
        transport_from({"PULSEBOARD_TRANSPORT": "sse"})


def test_credential_variables_are_reported_by_name() -> None:
    environ = {
        **ENVIRON,
        "PULSEBOARD_ACME_TOKEN": "x",
        "PULSEBOARD_CORP_PAT": "y",
        "PULSEBOARD_ACME_EMAIL": "a@example.com",
    }

    assert credential_variables(environ) == ["PULSEBOARD_ACME_TOKEN", "PULSEBOARD_CORP_PAT"]


# ---------------------------------------------------------------- CLI


def serve(tmp_path: Path, environ: Mapping[str, str]) -> tuple[int, str, str]:
    """``pulseboard serve`` with a settings file in ``tmp_path``; returns (code, out, err)."""
    config = tmp_path / "pulseboard.toml"
    data_dir = (tmp_path / "data").as_posix()
    config.write_text(f'data_dir = "{data_dir}"\n', encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--config", str(config), "serve"],
        console=Console(out=out, err=err),
        environ=environ,
        keychain=FakeKeychain(),
    )
    return code, out.getvalue(), err.getvalue()


def test_serve_without_transport_stays_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[tuple[object, ...]] = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append((a, k)))

    code, out, _ = serve(tmp_path, ENV)

    assert code == 0
    assert runs == [((), {})]  # FastMCP's default transport, stdio, exactly as before
    assert out == ""


def test_serve_with_http_transport_runs_the_hosted_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append(k))
    monkeypatch.setattr("pulseboard.hosted.server.configure_logging", lambda: None)
    environ = {**ENVIRON, "PULSEBOARD_PORT": "8080", "PULSEBOARD_JIRA_TOKEN": "leftover-secret"}

    code, out, err = serve(tmp_path, environ)

    assert code == 0
    assert [(r["transport"], r["stateless_http"], r["path"], r["port"]) for r in runs] == [
        ("http", True, "/mcp", 8080)
    ]
    assert runs[0]["uvicorn_config"] == {"log_config": None}  # root logging, redacted
    assert "PULSEBOARD_JIRA_TOKEN is set but ignored in HTTP mode" in err
    assert "leftover-secret" not in err
    assert out == ""


def test_serve_with_bad_http_settings_exits_2(tmp_path: Path) -> None:
    code, _, err = serve(tmp_path, {"PULSEBOARD_TRANSPORT": "http"})

    assert code == 2
    assert "PULSEBOARD_SITE_URL is required" in err
    assert "PULSEBOARD_PUBLIC_URL is required" in err


def test_serve_without_public_url_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[object] = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append(k))
    environ = {k: v for k, v in ENVIRON.items() if k != "PULSEBOARD_PUBLIC_URL"}

    code, _, err = serve(tmp_path, environ)

    assert code == 2
    assert runs == []  # never started
    assert "PULSEBOARD_PUBLIC_URL is required" in err


def test_serve_with_unknown_transport_exits_2(tmp_path: Path) -> None:
    code, _, err = serve(tmp_path, {"PULSEBOARD_TRANSPORT": "ws"})

    assert code == 2
    assert "'stdio' or 'http'" in err
