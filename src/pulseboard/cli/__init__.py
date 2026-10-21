"""The ``pulseboard`` command (DESIGN §9.9.4, §11.6).

``serve`` (the default) runs the stdio MCP server, or the hosted HTTP server when
``PULSEBOARD_TRANSPORT=http`` (ADDENDUM-01 §1A); ``site`` and ``config`` set Pulseboard up;
``admin purge`` applies retention. Standard output belongs to the MCP protocol while serving, so
``serve`` reports problems on standard error only.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime

from pulseboard import __version__
from pulseboard.app import EXPORTS_DIR, create_app, open_store, utc_now
from pulseboard.cli import site as site_commands
from pulseboard.cli.console import Console
from pulseboard.credentials import SystemKeychain, WritableKeychain
from pulseboard.domain.types import AccountId, SiteId
from pulseboard.errors import ConfigError
from pulseboard.hosted.config import Transport, transport_from
from pulseboard.settings import (
    CONFIG_PATH_ENV,
    Settings,
    SettingsLocation,
    default_config_file,
    default_data_dir,
    load_settings,
    locate_settings,
)
from pulseboard.store.repository import SnapshotStore

_NO_SITES_HINT = (
    "No Jira sites are configured, so no remote calls are made. Run `pulseboard site add`."
)


def _delete_exports(settings: Settings, paths: Sequence[str]) -> int:
    root = (settings.data_dir / EXPORTS_DIR).resolve()
    removed = 0
    for relative in paths:
        target = (root / relative).resolve()
        if target.is_relative_to(root) and target.is_file():
            target.unlink()
            removed += 1
    return removed


def purge(
    settings: Settings,
    site: str | None,
    account: str | None,
    *,
    store: SnapshotStore | None = None,
    now: datetime | None = None,
) -> str:
    """Delete expired rows and export files, or everything for one (site, account)."""
    store = store or open_store(settings)
    if site and account:
        counts = store.purge_account(SiteId(site), AccountId(account))
    else:
        counts = store.purge(now or utc_now(), settings.retention)
    files = _delete_exports(settings, counts.exports)
    return f"Removed {counts.snapshots} snapshots, {counts.plans} plans, {files} export files."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulseboard", description="Pulseboard MCP server")
    parser.add_argument(
        "--config",
        help=f"settings file (default: ${CONFIG_PATH_ENV}, else {default_config_file()})",
    )
    parser.add_argument("--version", action="version", version=f"pulseboard {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(
        "serve", help="run the MCP server: stdio (default), or HTTP with PULSEBOARD_TRANSPORT=http"
    )

    site = commands.add_parser("site", help="configure Jira sites")
    site_actions = site.add_subparsers(dest="site_command", required=True)
    site_actions.add_parser("add", help="guided setup of a site and its credentials")
    site_actions.add_parser("list", help="configured sites and where their credentials come from")
    test = site_actions.add_parser("test", help="read-only checks against one site")
    test.add_argument("site_id")
    remove = site_actions.add_parser("remove", help="remove a site and its keychain entries")
    remove.add_argument("site_id")

    config = commands.add_parser("config", help="inspect the settings file")
    config_actions = config.add_subparsers(dest="config_command", required=True)
    config_actions.add_parser("check", help="validate the settings file (no network calls)")
    config_actions.add_parser("path", help="show the settings file and data folder in use")

    admin = commands.add_parser("admin", help="administration")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    purge_parser = admin_commands.add_parser(
        "purge", help="apply retention and delete expired data"
    )
    purge_parser.add_argument("--site", help="delete everything for this site ID (with --account)")
    purge_parser.add_argument(
        "--account", help="delete everything for this account ID (with --site)"
    )
    return parser


def _config_path(location: SettingsLocation, console: Console) -> int:
    if location.path is None:
        console.say(f"Settings file: none ({location.reason})")
    else:
        console.say(f"Settings file: {location.path} ({location.reason})")
    try:
        data_dir = load_settings(location.path).data_dir
    except ConfigError:
        data_dir = default_data_dir()
    console.say(f"Data folder: {data_dir}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    console: Console | None = None,
    environ: Mapping[str, str] = os.environ,
    keychain: WritableKeychain | None = None,
    probe: site_commands.Probe = site_commands.default_probe,
) -> int:
    """Entry point of the ``pulseboard`` command."""
    parser = _parser()
    args = parser.parse_args(argv)
    console = console or Console()
    chain = keychain if keychain is not None else SystemKeychain()
    location = locate_settings(args.config, environ)

    if args.command == "config" and args.config_command == "path":
        return _config_path(location, console)
    creating = args.command == "site" and args.site_command == "add"
    path = location.path
    if creating and path is not None and not path.is_file():
        path = None  # site add creates the file
    try:
        settings = load_settings(path)
    except ConfigError as exc:
        console.warn(exc.message)
        return 2

    match args.command:
        case "config":
            sites = len(settings.sites)
            source = location.path or "defaults (no settings file)"
            console.say(f"Settings OK: {source}, {sites} site{'s' if sites != 1 else ''}.")
            if not sites:
                console.say(_NO_SITES_HINT)
            return 0
        case "site":
            return _site(args, settings, location, console, environ, chain, probe)
        case "admin":
            if bool(args.site) != bool(args.account):
                parser.error("--site and --account go together")
            console.say(purge(settings, args.site, args.account))
            return 0
        case _:
            transport = _transport(environ, console)
            if transport is None:
                return 2
            if transport is Transport.HTTP:
                # Imported here: the HTTP server needs the `hosted` extra; stdio does not.
                from pulseboard.hosted.server import serve  # noqa: PLC0415

                return serve(settings, console, environ)
            if not settings.sites:
                console.warn(_NO_SITES_HINT)
            create_app(settings, environ=environ, keychain=chain).server.run()
            return 0


def _transport(environ: Mapping[str, str], console: Console) -> Transport | None:
    try:
        return transport_from(environ)
    except ConfigError as exc:
        console.warn(exc.message)
        return None


def _site(  # noqa: PLR0913 - dispatch with the injected collaborators
    args: argparse.Namespace,
    settings: Settings,
    location: SettingsLocation,
    console: Console,
    environ: Mapping[str, str],
    keychain: WritableKeychain,
    probe: site_commands.Probe,
) -> int:
    match args.site_command:
        case "add":
            path = location.path or default_config_file()
            return site_commands.add(settings, path, console, environ, keychain, probe)
        case "list":
            return site_commands.list_sites(settings, console, environ, keychain)
        case "test":
            found = next((s for s in settings.sites if s.site_id == args.site_id), None)
            if found is None:
                console.warn(f"No site '{args.site_id}' is configured. See `pulseboard site list`.")
                return 2
            return site_commands.run_test(found, settings, console, environ, keychain, probe)
        case _:
            return site_commands.remove(settings, location.path, args.site_id, console, keychain)


if __name__ == "__main__":
    sys.exit(main())
