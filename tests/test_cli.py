"""The ``pulseboard`` command (DESIGN §9.9.4, §11.6). No real network calls: ``site test`` and
the ``site add`` check run against ``responses`` mocks, and the keychain is a fake."""

import io
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest
import responses
from pyrate_limiter import Duration, Limiter, Rate

from fakes import FakeKeychain
from pulseboard.cli import main
from pulseboard.cli.console import Console
from pulseboard.cli.site import derive_base_url, suggest_site_id
from pulseboard.credentials import KEYCHAIN_SERVICE, Credentials
from pulseboard.settings import CONFIG_PATH_ENV, Settings
from pulseboard.sites import Site
from pulseboard.sources.jira.client import JiraSource

CLOUD = "https://acme.atlassian.net"
DC = "https://jira.corp.example/jira"
FAST = Limiter(Rate(10_000, Duration.SECOND))
TOKEN = "tok-s3cr3t"  # noqa: S105 - a fake test token
SITES = f"""
[[sites]]
id = "acme"
base_url = "{CLOUD}"
deployment = "cloud"
auth = "api_token"

[[sites]]
id = "corp"
base_url = "{DC}"
deployment = "datacenter"
auth = "pat"

[[sites]]
id = "asf"
base_url = "https://issues.example.org/jira"
deployment = "datacenter"
auth = "anonymous"
contact = "ops@example.org"
"""


class Script:
    """Scripted answers for prompts; records every question asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        return self.answers.pop(0)


class Run:
    """One CLI invocation with captured output."""

    def __init__(self, tmp_path: Path, *answers: str, keychain: FakeKeychain | None = None) -> None:
        self.script = Script(*answers)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.console = Console(out=self.out, err=self.err, ask=self.script, ask_secret=self.script)
        self.keychain = keychain if keychain is not None else FakeKeychain()
        self.config = tmp_path / "pulseboard.toml"

    def __call__(self, *argv: str, environ: Mapping[str, str] | None = None) -> int:
        return main(
            ["--config", str(self.config), *argv],
            console=self.console,
            environ=environ or {},
            keychain=self.keychain,
            probe=fast_probe,
        )


def fast_probe(site: Site, credentials: Credentials, settings: Settings) -> JiraSource:
    return JiraSource(
        site,
        credentials,
        settings.for_site(site).limits,
        cache_enabled=False,
        limiter=FAST,
        sleep=lambda _: None,
        anonymous_time_zone=settings.team.time_zone,
    )


def write_settings(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "pulseboard.toml"
    data_dir = (tmp_path / "data").as_posix()
    config.write_text(f'data_dir = "{data_dir}"\n{extra}', encoding="utf-8")
    return config


# ---------------------------------------------------------------- config and serve


def test_invalid_config_exits_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "missing.toml"), "admin", "purge"]) == 2
    assert "Settings file not found" in capsys.readouterr().err


def test_config_check(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    run = Run(tmp_path)

    assert run("config", "check") == 0
    assert "Settings OK" in run.out.getvalue()
    assert "3 sites" in run.out.getvalue()


def test_config_check_names_the_site_and_field(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES.replace('auth = "pat"', 'auth = "api_token"'))
    run = Run(tmp_path)

    assert run("config", "check") == 2
    assert "sites[corp]" in run.err.getvalue()


def test_config_check_without_sites_hints_site_add(tmp_path: Path) -> None:
    write_settings(tmp_path)
    run = Run(tmp_path)

    assert run("config", "check") == 0
    assert "pulseboard site add" in run.out.getvalue()


def test_config_path_shows_file_reason_and_data_folder(tmp_path: Path) -> None:
    write_settings(tmp_path)
    run = Run(tmp_path)

    assert run("config", "path") == 0
    out = run.out.getvalue()
    assert f"Settings file: {run.config} (--config option)" in out
    assert f"Data folder: {tmp_path / 'data'}" in out


def test_config_path_from_environment_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path)
    environ = {CONFIG_PATH_ENV: str(tmp_path / "pb.toml")}

    assert main(["config", "path"], console=run.console, environ=environ) == 0
    assert "environment variable" in run.out.getvalue()

    monkeypatch.setattr("pulseboard.settings.default_config_file", lambda: tmp_path / "none.toml")
    assert main(["config", "path"], console=run.console, environ={}) == 0
    assert "Settings file: none (no settings file found" in run.out.getvalue()


def test_serve_without_sites_warns_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_settings(tmp_path)
    started: list[Settings] = []

    class FakeServer:
        def run(self) -> None:
            started.append(settings_seen[0])

    class FakeApp:
        server = FakeServer()

    settings_seen: list[Settings] = []

    def fake_create_app(settings: Settings, **_: object) -> FakeApp:
        settings_seen.append(settings)
        return FakeApp()

    monkeypatch.setattr("pulseboard.cli.create_app", fake_create_app)
    run = Run(tmp_path)

    assert run() == 0
    assert started
    assert "No Jira sites are configured" in run.err.getvalue()
    assert run.out.getvalue() == ""  # stdout belongs to the MCP protocol


# ---------------------------------------------------------------- admin purge


def test_purge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exports = tmp_path / "data" / "exports"
    exports.mkdir(parents=True)

    assert main(["--config", str(write_settings(tmp_path)), "admin", "purge"]) == 0
    assert capsys.readouterr().out.startswith("Removed 0 snapshots, 0 plans, 0 export files.")
    assert (tmp_path / "data" / "pulseboard.db").is_file()


def test_purge_account(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["admin", "purge", "--site", "acme", "--account", "a"]

    assert main(["--config", str(write_settings(tmp_path)), *args]) == 0
    assert "Removed 0 snapshots" in capsys.readouterr().out


def test_purge_needs_site_and_account_together(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--config", str(write_settings(tmp_path)), "admin", "purge", "--site", "acme"])


# ---------------------------------------------------------------- site list


def test_site_list_shows_credential_source_never_the_value(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    keychain = FakeKeychain({(KEYCHAIN_SERVICE, "corp:token"): TOKEN})
    run = Run(tmp_path, keychain=keychain)

    environ = {"PULSEBOARD_ACME_EMAIL": "a@b.c", "PULSEBOARD_ACME_TOKEN": TOKEN}
    assert run("site", "list", environ=environ) == 0

    lines = run.out.getvalue().splitlines()
    assert "credentials=environment" in lines[0]
    assert "credentials=keychain" in lines[1]
    assert "credentials=not needed" in lines[2]
    assert "profile=polite" in lines[2]
    assert TOKEN not in run.out.getvalue()


def test_site_list_reports_missing_credentials(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    run = Run(tmp_path)

    assert run("site", "list") == 0
    assert "credentials=missing" in run.out.getvalue().splitlines()[0]


def test_site_list_without_sites(tmp_path: Path) -> None:
    write_settings(tmp_path)
    run = Run(tmp_path)

    assert run("site", "list") == 0
    assert "pulseboard site add" in run.out.getvalue()


# ---------------------------------------------------------------- site add


def test_site_add_cloud_stores_secrets_in_the_keychain(tmp_path: Path) -> None:
    write_settings(tmp_path)
    run = Run(
        tmp_path,
        f"{CLOUD}/jira/software/projects/ABC/boards/42",  # link
        "",  # base URL: accept the proposal
        "",  # deployment: cloud
        "",  # site ID: acme
        "n",  # no server-info check
        "dev@example.com",
        TOKEN,
        "n",  # no site test
    )

    assert run("site", "add") == 0

    text = run.config.read_text(encoding="utf-8")
    site = tomllib.loads(text)["sites"][0]
    assert site == {"id": "acme", "base_url": CLOUD, "deployment": "cloud", "auth": "api_token"}
    assert TOKEN not in text
    assert TOKEN not in run.out.getvalue() + run.err.getvalue()
    assert run.keychain.entries == {
        (KEYCHAIN_SERVICE, "acme:email"): "dev@example.com",
        (KEYCHAIN_SERVICE, "acme:token"): TOKEN,
    }
    assert "Base URL [https://acme.atlassian.net]: " in run.script.questions


def test_site_add_data_center_without_keychain_prints_env_names(tmp_path: Path) -> None:
    run = Run(
        tmp_path,
        f"{DC}/secure/RapidBoard.jspa?rapidView=7",
        "",  # base URL: https://jira.corp.example/jira
        "",  # deployment: datacenter
        "",  # auth: pat
        "corp-dc",
        "n",
        "n",
        keychain=FakeKeychain(available=False),
    )

    assert run("site", "add") == 0

    site = tomllib.loads(run.config.read_text(encoding="utf-8"))["sites"][0]
    assert site["base_url"] == DC
    assert site["deployment"] == "datacenter"
    assert "PULSEBOARD_CORP_DC_TOKEN" in run.out.getvalue()
    assert run.keychain.entries == {}


def test_site_add_anonymous_asks_for_a_contact(tmp_path: Path) -> None:
    write_settings(tmp_path)
    run = Run(
        tmp_path,
        "https://issues.example.org/jira/browse/ABC-1",
        "",
        "",
        "anonymous",
        "asf",
        "ops@example.org",
        "n",
        "n",
    )

    assert run("site", "add") == 0

    site = tomllib.loads(run.config.read_text(encoding="utf-8"))["sites"][-1]
    assert site["profile"] == "polite"
    assert site["contact"] == "ops@example.org"
    assert not any("Token" in q for q in run.script.questions)


@pytest.mark.parametrize(
    "answers",
    [
        ("ftp://jira.example",),
        (f"{CLOUD}/x", "", "mainframe"),
        (f"{DC}/browse/X-1", "", "", "kerberos"),
        (f"{CLOUD}/x", "", "datacenter", "", "acme"),  # Cloud host marked datacenter
    ],
)
def test_site_add_rejects_bad_answers(tmp_path: Path, answers: tuple[str, ...]) -> None:
    write_settings(tmp_path)
    run = Run(tmp_path, *answers)

    assert run("site", "add") == 2
    assert "[[sites]]" not in run.config.read_text(encoding="utf-8")


def test_site_add_rejects_a_duplicate(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    before = (tmp_path / "pulseboard.toml").read_text(encoding="utf-8")
    run = Run(tmp_path, f"{CLOUD}/x", "", "", "acme-2", "n")

    assert run("site", "add") == 2
    assert "only once" in run.err.getvalue()
    assert run.config.read_text(encoding="utf-8") == before


@responses.activate
def test_site_add_can_verify_the_deployment(tmp_path: Path) -> None:
    write_settings(tmp_path)
    responses.get(f"{DC}/rest/api/2/serverInfo", json={"deploymentType": "Cloud"})
    run = Run(tmp_path, f"{DC}/browse/X-1", "", "", "", "corp", "y", TOKEN, "n")

    assert run("site", "add") == 0

    assert "reports 'cloud', not 'datacenter'" in run.err.getvalue()
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_site_add_then_test(tmp_path: Path) -> None:
    write_settings(tmp_path)
    responses.get(f"{CLOUD}/rest/api/2/serverInfo", json={"deploymentType": "Cloud"})
    responses.get(f"{CLOUD}/rest/api/2/myself", json={"accountId": "a", "timeZone": "UTC"})
    run = Run(tmp_path, f"{CLOUD}/x", "", "", "", "n", "dev@example.com", TOKEN, "")

    assert run("site", "add") == 0
    assert "PASS  sign-in" in run.out.getvalue()


@pytest.mark.parametrize(
    ("link", "base"),
    [
        (f"{CLOUD}/jira/software/projects/ABC/boards/42", CLOUD),
        (f"{CLOUD}/", CLOUD),
        (f"{DC}/secure/RapidBoard.jspa?rapidView=7", DC),
        ("https://jira.corp.example:8443/browse/ABC-1", "https://jira.corp.example:8443"),
        (
            "https://jira.corp.example/tools/jira/projects/ABC",
            "https://jira.corp.example/tools/jira",
        ),
        (f"{DC}/", DC),
    ],
)
def test_derive_base_url(link: str, base: str) -> None:
    assert derive_base_url(link) == base


def test_suggest_site_id() -> None:
    assert suggest_site_id(CLOUD, set()) == "acme"
    assert suggest_site_id(CLOUD, {"acme", "acme-2"}) == "acme-3"
    assert suggest_site_id("https://my_jira.example", set()) == "my-jira"
    assert suggest_site_id("https://x.example", set()) == "x0"


# ---------------------------------------------------------------- site test


@responses.activate
def test_site_test_passes(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    responses.get(
        f"{DC}/rest/api/2/serverInfo", json={"deploymentType": "DataCenter", "version": "9.12.4"}
    )
    responses.get(
        f"{DC}/rest/api/2/myself",
        json={"key": "JIRAUSER1", "displayName": "Dev", "timeZone": "Asia/Tokyo"},
    )
    run = Run(tmp_path)

    assert run("site", "test", "corp", environ={"PULSEBOARD_CORP_TOKEN": TOKEN}) == 0

    out = run.out.getvalue()
    assert "PASS  reachable: https://jira.corp.example/jira answers (Jira 9.12.4)" in out
    assert "PASS  deployment" in out
    assert "PASS  sign-in: signed in as Dev" in out
    assert "PASS  time zone: Asia/Tokyo" in out
    assert responses.calls[1].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in out


@responses.activate
def test_site_test_reports_failures_with_hints(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    responses.get(f"{CLOUD}/rest/api/2/serverInfo", json={"deploymentType": "Server"})
    responses.get(f"{CLOUD}/rest/api/2/myself", status=401)
    run = Run(tmp_path)
    environ = {"PULSEBOARD_ACME_EMAIL": "a@b.c", "PULSEBOARD_ACME_TOKEN": TOKEN}

    assert run("site", "test", "acme", environ=environ) == 1

    out = run.out.getvalue()
    assert "FAIL  deployment: site reports 'server'; the settings say 'cloud'" in out
    assert "FAIL  sign-in: Jira rejected the credentials" in out


@responses.activate
def test_site_test_missing_credentials(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    responses.get(f"{CLOUD}/rest/api/2/serverInfo", json={"deploymentType": "Cloud"})
    run = Run(tmp_path)

    assert run("site", "test", "acme") == 1
    assert "FAIL  sign-in: No email configured" in run.out.getvalue()
    assert "PULSEBOARD_ACME_EMAIL" in run.out.getvalue()


@responses.activate
def test_site_test_unreachable(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    responses.get(f"{CLOUD}/rest/api/2/serverInfo", status=503)
    run = Run(tmp_path)

    assert run("site", "test", "acme") == 1
    assert run.out.getvalue().startswith("FAIL  reachable")


@responses.activate
def test_site_test_polite_site_checks_robots(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    responses.get(
        "https://issues.example.org/robots.txt",
        body="User-agent: *\nDisallow: /jira/rest/api/2/search\n",
    )
    responses.get(
        "https://issues.example.org/jira/rest/api/2/serverInfo", json={"deploymentType": "Server"}
    )
    run = Run(tmp_path)

    assert run("site", "test", "asf") == 1

    out = run.out.getvalue()
    assert "PASS  time zone: UTC" in out
    assert "FAIL  robots.txt: disallows rest/api/2/search" in out
    assert "sign-in" not in out
    assert all("Authorization" not in c.request.headers for c in responses.calls)


def test_site_test_unknown_site(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    run = Run(tmp_path)

    assert run("site", "test", "nope") == 2
    assert "pulseboard site list" in run.err.getvalue()


# ---------------------------------------------------------------- site remove


def test_site_remove_and_keychain_cleanup(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    keychain = FakeKeychain({(KEYCHAIN_SERVICE, "corp:token"): TOKEN})
    run = Run(tmp_path, "y", keychain=keychain)

    assert run("site", "remove", "corp") == 0

    sites = tomllib.loads(run.config.read_text(encoding="utf-8"))["sites"]
    assert [s["id"] for s in sites] == ["acme", "asf"]
    assert keychain.entries == {}
    assert "snapshots remain" in run.out.getvalue()


def test_site_remove_keeps_keychain_unless_confirmed(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    keychain = FakeKeychain({(KEYCHAIN_SERVICE, "corp:token"): TOKEN})
    run = Run(tmp_path, "", keychain=keychain)

    assert run("site", "remove", "corp") == 0
    assert keychain.entries


def test_site_remove_unknown(tmp_path: Path) -> None:
    write_settings(tmp_path, SITES)
    run = Run(tmp_path)

    assert run("site", "remove", "nope") == 2
