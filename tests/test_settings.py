"""Settings: file location, TOML file, environment overrides and validation (ADR-017, §9.9)."""

from pathlib import Path

import pytest

from pulseboard.domain.types import RequestProfile
from pulseboard.errors import ConfigError
from pulseboard.settings import (
    CONFIG_PATH_ENV,
    PointsMode,
    Settings,
    default_config_file,
    load_settings,
    locate_settings,
)

VALID = """
[[sites]]
id = "acme"
base_url = "https://acme.atlassian.net"
deployment = "cloud"
auth = "api_token"

[[sites]]
id = "lab"
base_url = "http://jira.lab.local:8080/jira"
deployment = "datacenter"
auth = "pat"
allow_http = true

[[sites]]
id = "asf"
base_url = "https://issues.apache.org/jira"
deployment = "datacenter"
auth = "anonymous"
contact = "mailto-ops@example.org"

[team]
time_zone = "Europe/Berlin"
default_holiday_country = "DE"
extra_non_working_dates = [2026-12-24]

[team.points]
mode = "fixed"
fixed_points_per_day = 1.5

[[team.people]]
account_id = "abc-123"
capacity = 0.5
holiday_country = "FR"

[[team.leave]]
account_id = "abc-123"
start = 2026-11-02
end = 2026-11-06

[limits]
max_open_issues = 1500
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pulseboard.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_loads(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, VALID))

    assert [s.site_id for s in settings.sites] == ["acme", "lab", "asf"]
    assert settings.team.time_zone == "Europe/Berlin"
    assert settings.team.points.mode is PointsMode.FIXED
    assert settings.team.people[0].capacity == 0.5
    assert settings.team.leave[0].end.day == 6
    assert settings.limits.max_open_issues == 1500
    assert settings.limits.page_size == 20  # default kept


def test_anonymous_sites_default_to_the_polite_profile(tmp_path: Path) -> None:
    acme, lab, asf = load_settings(write(tmp_path, VALID)).sites

    assert acme.profile is RequestProfile.STANDARD
    assert lab.profile is RequestProfile.STANDARD
    assert asf.profile is RequestProfile.POLITE
    assert not asf.needs_credentials


def test_defaults_match_design() -> None:
    settings = load_settings(None)

    assert settings.sites == ()
    assert settings.limits.max_open_issues == 2000
    assert settings.limits.max_boards_per_program == 10
    assert settings.limits.tool_timeout_s == 120
    assert settings.limits.stale_plan_max_hours == 24
    assert settings.limits.requests_per_second == 4
    assert settings.limits.max_concurrent_requests_per_site == 4
    assert settings.team.estimated_threshold == 0.6
    assert settings.retention.exports_days == 7
    assert settings.token_budgets.summary == 500


def test_polite_profile_tightens_limits(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, VALID))
    acme, _, asf = settings.sites

    assert settings.for_site(acme) is settings
    polite = settings.for_site(asf)
    assert polite.limits.requests_per_second == 1
    assert polite.limits.max_concurrent_requests_per_site == 1
    assert polite.limits.max_open_issues == 500
    assert polite.limits.reporting_window_days == 30
    assert polite.limits.full_reload_every == 50
    assert polite.limits.refresh_interval_s == 900
    assert polite.limits.max_consecutive_429 == 3
    assert polite.http_cache.configuration_ttl_s == 24 * 3600
    assert polite.team == settings.team


def test_polite_profile_never_loosens_a_stricter_limit(tmp_path: Path) -> None:
    text = VALID.replace("max_open_issues = 1500", "max_open_issues = 100\nmax_consecutive_429 = 2")
    settings = load_settings(write(tmp_path, text))

    polite = settings.for_site(settings.sites[2])

    assert polite.limits.max_open_issues == 100
    assert polite.limits.max_consecutive_429 == 2


def test_environment_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEBOARD_TEAM__TIME_ZONE", "Asia/Tokyo")
    monkeypatch.setenv("PULSEBOARD_LIMITS__PAGE_SIZE", "50")
    monkeypatch.setenv("PULSEBOARD_ACME_TOKEN", "not-a-setting")  # credential variables are ignored

    settings = load_settings(write(tmp_path, VALID))

    assert settings.team.time_zone == "Asia/Tokyo"
    assert settings.team.default_holiday_country == "DE"  # rest of the section from the file
    assert settings.limits.page_size == 50


def test_settings_are_frozen() -> None:
    settings = load_settings(None)
    with pytest.raises(ValueError, match="frozen"):
        settings.limits = settings.limits  # type: ignore[misc]


@pytest.mark.parametrize(
    ("anchor", "key"),
    [
        ('auth = "api_token"', "token"),
        ('auth = "api_token"', "api_token"),
        ('auth = "api_token"', "password"),
        ('auth = "api_token"', "email"),
        ('auth = "pat"', "pat"),
        ('auth = "pat"', "secret"),
        ("[limits]", "api_key"),
        ("[team]", "password"),
    ],
)
def test_secret_looking_fields_anywhere_are_rejected(tmp_path: Path, anchor: str, key: str) -> None:
    text = VALID.replace(anchor, f'{anchor}\n{key} = "s3cr3t"', 1)

    with pytest.raises(ConfigError) as info:
        load_settings(write(tmp_path, text))

    assert "must not be in the settings file" in info.value.message
    assert "keychain" in info.value.message
    assert "s3cr3t" not in info.value.message


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ('time_zone = "Europe/Berlin"', 'time_zone = "Mars/Olympus"', "IANA time zone"),
        ('default_holiday_country = "DE"', 'default_holiday_country = "XX"', "holiday country"),
        ("allow_http = true", "allow_http = false", "must use https"),
        ('auth = "pat"', 'auth = "api_token"', "sites[lab]: Value error, A datacenter site"),
        ('auth = "api_token"', 'auth = "pat"', "sites[acme]: Value error, A cloud site"),
        ('auth = "api_token"', 'auth = "oauth_3lo"', "hosted mode"),
        ('id = "lab"', 'id = "acme"', "only once"),
        ('id = "lab"', 'id = "Lab_1"', "sites[Lab_1].id"),
        ('id = "lab"', 'id = "l"', "at least 2"),
        ('id = "lab"', 'id = "lab-"', "sites[lab-].id"),
        ('id = "lab"', f'id = "{"x" * 33}"', "at most 32"),
        (
            '"http://jira.lab.local:8080/jira"',
            '"https://issues.apache.org/jira/"',
            "base_url may appear only once",
        ),
        ('deployment = "cloud"', 'deployment = "datacenter"', "is a Jira Cloud host"),
        (
            '"http://jira.lab.local:8080/jira"\ndeployment = "datacenter"',
            '"http://jira.lab.local:8080/jira"\ndeployment = "cloud"',
            "not a Jira Cloud host",
        ),
        ('contact = "mailto-ops@example.org"', "", "needs a contact"),
        ('contact = "mailto-ops@example.org"', 'contact = "ops team"', "without spaces"),
        ('contact = "mailto-ops@example.org"', 'contact = "ops"', "URL or an email"),
        (
            'contact = "mailto-ops@example.org"',
            'contact = "a@b.org"\nprofile = "standard"',
            "must use profile = 'polite'",
        ),
        ("max_open_issues = 1500", "max_open_issues = 0", "max_open_issues"),
        ("end = 2026-11-06", "end = 2026-11-01", "on or after"),
        ("fixed_points_per_day = 1.5", "", "fixed_points_per_day"),
        ("[limits]", "[limits]\nunknown_limit = 3", "unknown_limit"),
        ('auth = "api_token"', 'auth = "api_token"\nflavour = "x"', "sites[acme].flavour"),
        (
            '"https://acme.atlassian.net"',
            '"https://acme.atlassian.net/?x=1"',
            "query string",
        ),
    ],
)
def test_invalid_settings_fail_with_clear_message(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    assert old in VALID
    with pytest.raises(ConfigError) as info:
        load_settings(write(tmp_path, VALID.replace(old, new, 1)))

    assert expected in info.value.message
    assert info.value.message.startswith("Invalid settings in")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found.*pulseboard site add"):
        load_settings(tmp_path / "absent.toml")


def test_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(write(tmp_path, "[sites\n"))


def test_location_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_file = tmp_path / "user" / "pulseboard.toml"
    monkeypatch.setattr("pulseboard.settings.default_config_file", lambda: user_file)
    environ = {CONFIG_PATH_ENV: "/etc/pb.toml"}

    assert locate_settings("cli.toml", environ) == (Path("cli.toml"), "--config option")
    assert locate_settings(None, environ).path == Path("/etc/pb.toml")
    missing = locate_settings(None, {})
    assert missing.path is None
    assert str(user_file) in missing.reason

    user_file.parent.mkdir()
    write(user_file.parent, VALID)
    assert locate_settings(None, {}) == (user_file, "per-user configuration folder")


def test_working_directory_file_is_not_picked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, VALID)
    monkeypatch.setattr(
        "pulseboard.settings.default_config_file", lambda: tmp_path / "x" / "p.toml"
    )

    assert locate_settings(None, {}).path is None


def test_default_folders_follow_platform_conventions() -> None:
    assert default_config_file().name == "pulseboard.toml"
    assert "pulseboard" in default_config_file().parent.parts[-1].lower()
    assert "pulseboard" in str(Settings().data_dir).lower()


def test_shipped_example_is_valid() -> None:
    example = Path(__file__).parents[1] / "examples" / "pulseboard.example.toml"

    settings = load_settings(example)

    assert [s.site_id for s in settings.sites] == ["acme-cloud", "corp-dc", "asf"]
    assert settings.sites[2].profile is RequestProfile.POLITE


def test_site_origin_is_normalized(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, VALID))
    lab = settings.sites[1]

    assert str(lab.origin) == "http://jira.lab.local:8080/jira"
    assert isinstance(settings, Settings)
