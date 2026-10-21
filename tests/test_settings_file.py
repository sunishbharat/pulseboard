"""Settings-file edits for ``site add`` / ``site remove``: validated, comment-preserving."""

from pathlib import Path

import pytest

from pulseboard.errors import ConfigError
from pulseboard.settings_file import add_site, remove_site

CLOUD = {
    "id": "acme",
    "base_url": "https://acme.atlassian.net",
    "deployment": "cloud",
    "auth": "api_token",
}
EXISTING = """# my team settings
[team]
time_zone = "Europe/Berlin"  # keep me
"""


def test_creates_the_file_and_folders(tmp_path: Path) -> None:
    path = tmp_path / "config" / "pulseboard.toml"

    settings = add_site(path, CLOUD)

    assert [s.site_id for s in settings.sites] == ["acme"]
    assert path.is_file()


def test_keeps_comments_and_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "pulseboard.toml"
    path.write_text(EXISTING, encoding="utf-8")

    add_site(path, CLOUD)
    settings = add_site(path, {**CLOUD, "id": "beta", "base_url": "https://beta.atlassian.net"})

    text = path.read_text(encoding="utf-8")
    assert "# my team settings" in text
    assert "# keep me" in text
    assert settings.team.time_zone == "Europe/Berlin"
    assert [s.site_id for s in settings.sites] == ["acme", "beta"]


def test_invalid_entry_leaves_the_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "pulseboard.toml"
    path.write_text(EXISTING, encoding="utf-8")

    with pytest.raises(ConfigError, match="Jira Cloud host"):
        add_site(path, {**CLOUD, "deployment": "datacenter", "auth": "pat"})

    assert path.read_text(encoding="utf-8") == EXISTING
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pulseboard.toml"]


def test_a_secret_is_never_written(tmp_path: Path) -> None:
    path = tmp_path / "pulseboard.toml"

    with pytest.raises(ConfigError, match="must not be in the settings file"):
        add_site(path, {**CLOUD, "token": "s3cr3t"})

    assert not path.exists()


def test_remove(tmp_path: Path) -> None:
    path = tmp_path / "pulseboard.toml"
    path.write_text(EXISTING, encoding="utf-8")
    add_site(path, CLOUD)

    assert remove_site(path, "acme")
    assert not remove_site(path, "acme")
    assert "[[sites]]" not in path.read_text(encoding="utf-8")
    assert "# keep me" in path.read_text(encoding="utf-8")


def test_remove_without_a_file(tmp_path: Path) -> None:
    assert not remove_site(tmp_path / "absent.toml", "acme")
    (tmp_path / "plain.toml").write_text(EXISTING, encoding="utf-8")
    assert not remove_site(tmp_path / "plain.toml", "acme")
