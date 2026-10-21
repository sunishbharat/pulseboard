"""Edits to the settings file for ``pulseboard site add`` and ``site remove`` (DESIGN §9.9.4).

``tomlkit`` keeps the user's comments and layout. Every edit is written to a temporary file and
validated with ``load_settings`` before it replaces the original, so a failed edit never leaves
an invalid file behind. Secrets are never written: ``Site`` rejects credential fields.
"""

from collections.abc import Mapping
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Array

from pulseboard.settings import Settings, load_settings

SITES_KEY = "sites"


def _read(path: Path) -> tomlkit.TOMLDocument:
    if path.is_file():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _write_validated(path: Path, document: tomlkit.TOMLDocument) -> Settings:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.new")
    staged.write_text(tomlkit.dumps(document), encoding="utf-8")
    try:
        settings = load_settings(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    staged.replace(path)
    return settings


def add_site(path: Path, entry: Mapping[str, str | bool]) -> Settings:
    """Append a ``[[sites]]`` entry, creating the file if needed.

    Raises:
        ConfigError: if the result would be invalid; the file is then left unchanged.
    """
    document = _read(path)
    table = tomlkit.table()
    for key, value in entry.items():
        table[key] = value
    sites = document.get(SITES_KEY)
    if isinstance(sites, AoT | Array):
        sites.append(table)
    else:
        document[SITES_KEY] = tomlkit.aot()
        document[SITES_KEY].append(table)
    return _write_validated(path, document)


def remove_site(path: Path, site_id: str) -> bool:
    """Remove the site entry with ``id = site_id``; ``False`` if the file has no such entry.

    Raises:
        ConfigError: if the result would be invalid; the file is then left unchanged.
    """
    if not path.is_file():
        return False
    document = _read(path)
    sites = document.get(SITES_KEY)
    if not isinstance(sites, AoT | Array):
        return False
    matches = [i for i, s in enumerate(sites) if isinstance(s, Mapping) and s.get("id") == site_id]
    if not matches:
        return False
    del sites[matches[0]]
    if not len(sites):
        del document[SITES_KEY]
    _write_validated(path, document)
    return True
