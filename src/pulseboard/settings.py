"""Typed settings: a TOML file read by pydantic-settings, plus environment overrides (ADR-017).

The file is found by ``--config``, then ``$PULSEBOARD_CONFIG``, then the per-user configuration
folder (DESIGN §9.9.1). Credentials are never part of the settings; see ``pulseboard.credentials``.
"""

import os
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NamedTuple, Self

import holidays
import platformdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pulseboard.domain.types import AccountId, RequestProfile, WarningId, jira_name_key
from pulseboard.errors import ConfigError
from pulseboard.sites import CREDENTIAL_HINT, CREDENTIAL_KEYS, Site
from pulseboard.timezones import TimeZoneName

APP_NAME = "pulseboard"
CONFIG_PATH_ENV = "PULSEBOARD_CONFIG"
DEFAULT_CONFIG_FILE = "pulseboard.toml"

_SETTINGS = ConfigDict(frozen=True, extra="forbid")
_SITE_ID_ECHO_MAX_CHARS = 40
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
Fraction = Annotated[float, Field(gt=0, le=1)]


def _check_holiday_country(code: str) -> str:
    if code not in holidays.list_supported_countries():
        msg = f"'{code}' is not a supported holiday country code (ISO 3166-1 alpha-2)."
        raise ValueError(msg)
    return code


# ---------------------------------------------------------------- team


class PointsMode(StrEnum):
    """How story points convert to working time (ADR-004)."""

    VELOCITY = "velocity"
    FIXED = "fixed"


class UnassignedPolicy(StrEnum):
    """How unassigned tasks are scheduled (DESIGN §10.3)."""

    SEPARATE_LANE = "separate_lane"
    AUTO_ASSIGN = "auto_assign"


class PriorityMode(StrEnum):
    """Whether board rank constrains each person's task order (DESIGN §10.3)."""

    BOARD_ORDER = "board_order"
    OPTIMIZED = "optimized"


class PointsPolicy(BaseModel):
    """Story points to working time."""

    model_config = _SETTINGS

    mode: PointsMode = PointsMode.VELOCITY
    velocity_sprints: PositiveInt = 3
    fallback_points_per_day: PositiveFloat = 1.0
    """Used in velocity mode when no closed sprint has completed points."""
    fixed_points_per_day: PositiveFloat | None = None

    @model_validator(mode="after")
    def _fixed_needs_rate(self) -> Self:
        if self.mode is PointsMode.FIXED and self.fixed_points_per_day is None:
            msg = "mode = 'fixed' needs fixed_points_per_day."
            raise ValueError(msg)
        return self


class PersonSettings(BaseModel):
    """Per-person overrides of the team defaults."""

    model_config = _SETTINGS

    account_id: AccountId
    capacity: Fraction | None = None
    holiday_country: str | None = None

    _country = field_validator("holiday_country")(
        lambda v: v if v is None else _check_holiday_country(v)
    )


class LeavePeriod(BaseModel):
    """A person's leave, inclusive of both dates."""

    model_config = _SETTINGS

    account_id: AccountId
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            msg = "Leave end must be on or after its start."
            raise ValueError(msg)
        return self


class LinkBlocker(StrEnum):
    """Which end of a dependency link is the blocker (ADDENDUM-03 §3)."""

    INWARD = "inward"
    """The issue named by the type's inward text blocks this one (``Blocks``: "is blocked by")."""
    OUTWARD = "outward"
    """The issue named by the type's outward text blocks this one (e.g. "depends on")."""


DEFAULT_LINK_TYPE = "Blocks"


class LinkTypeSetting(BaseModel):
    """A Jira issue link type that schedules as a dependency, and which end is the blocker.

    A plain name is accepted for ``Blocks`` only (its blocker is the inward end); any other
    type must say which end blocks, because Jira link types have no fixed direction.
    """

    model_config = _SETTINGS

    name: Annotated[str, Field(min_length=1)]
    blocker: LinkBlocker

    @model_validator(mode="before")
    @classmethod
    def _from_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if jira_name_key(value) != jira_name_key(DEFAULT_LINK_TYPE):
            msg = (
                f"Link type '{value.strip()}' needs a direction: write "
                f'{{name = "{value.strip()}", blocker = "inward"}} or blocker = "outward" '
                "(the end whose issue must finish first)."
            )
            raise ValueError(msg)
        return {"name": value, "blocker": LinkBlocker.INWARD}

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if not value.strip():
            msg = "A link type name cannot be blank."
            raise ValueError(msg)
        return value.strip()


class SolverSettings(BaseModel):
    """Deterministic solver budget (DESIGN §10.4)."""

    model_config = _SETTINGS

    deterministic_time: PositiveFloat = 1.0
    """CP-SAT deterministic work budget; independent of CPU speed (ADR-019)."""
    wallclock_cap_s: PositiveFloat = 60.0
    """Safety cap only; reaching it raises warning W012."""
    seed: int = 0


class TeamSettings(BaseModel):
    """Team calendar, effort and scheduling policy (DESIGN §8, §10)."""

    model_config = _SETTINGS

    time_zone: TimeZoneName = "UTC"
    default_holiday_country: str = "US"
    hours_per_day: Annotated[float, Field(gt=0, le=24)] = 8.0
    default_capacity: Fraction = 1.0
    points: PointsPolicy = PointsPolicy()
    default_effort_hours: PositiveFloat = 8.0
    estimated_threshold: Fraction = 0.6
    """Below this share of estimated open issues, Monte Carlo is the primary forecast."""
    high_confidence_threshold: Fraction = 0.9
    """At or above this share of estimated open issues, a schedule forecast is high confidence."""
    people: tuple[PersonSettings, ...] = ()
    leave: tuple[LeavePeriod, ...] = ()
    extra_non_working_dates: tuple[date, ...] = ()
    unassigned_policy: UnassignedPolicy = UnassignedPolicy.SEPARATE_LANE
    priority_mode: PriorityMode = PriorityMode.BOARD_ORDER
    dependency_link_types: tuple[LinkTypeSetting, ...] = (
        LinkTypeSetting(name=DEFAULT_LINK_TYPE, blocker=LinkBlocker.INWARD),
    )
    """Link types scheduled as dependencies; names match Jira's trimmed and case-insensitively."""
    include_subtasks: bool = True
    solver: SolverSettings = SolverSettings()

    _country = field_validator("default_holiday_country")(_check_holiday_country)

    @model_validator(mode="after")
    def _unique_people(self) -> Self:
        ids = [p.account_id for p in self.people]
        if len(ids) != len(set(ids)):
            msg = "Each account_id may appear only once in people."
            raise ValueError(msg)
        names = [jira_name_key(t.name) for t in self.dependency_link_types]
        if len(names) != len(set(names)):
            msg = "Each link type may appear only once in dependency_link_types."
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------- limits and budgets


class Limits(BaseModel):
    """Limits, intervals and windows (DESIGN §2.3, §9, §10.7, §12)."""

    model_config = _SETTINGS

    max_open_issues: PositiveInt = 2000
    max_boards_per_program: PositiveInt = 10
    tool_timeout_s: PositiveFloat = 120.0
    refresh_interval_s: PositiveFloat = 120.0
    full_reload_every: PositiveInt = 15
    incremental_overlap_minutes: PositiveInt = 5
    stale_plan_max_hours: PositiveFloat = 24.0
    reporting_window_days: PositiveInt = 90
    throughput_history_weeks: PositiveInt = 12
    min_history_weeks: PositiveInt = 4
    monte_carlo_trials: PositiveInt = 10_000
    page_size: PositiveInt = 20
    max_concurrent_requests_per_site: PositiveInt = 4
    """Calls to one site are sequential today, which stays within this limit."""
    requests_per_second: PositiveFloat = 4.0
    """Client-side throttle per site (DESIGN §9.8)."""
    jira_timeout_s: PositiveFloat = 30.0
    """Connect and read timeout for each Jira call."""
    max_retries: PositiveInt = 4
    """Retries for throttled (429) or failed (5xx, connection) Jira calls."""
    retry_max_wait_s: PositiveFloat = 60.0
    """Longest wait between retries, including a server's Retry-After."""
    retry_base_wait_s: PositiveFloat = 1.0
    """First backoff wait; it doubles on each retry (unless Retry-After says otherwise)."""
    max_consecutive_429: PositiveInt | None = None
    """Stop with ``rate_limited`` after this many 429 responses in a row (None: retry limit)."""
    jira_page_size: PositiveInt = 100
    planning_horizon_years: PositiveInt = 3
    """How far ahead holiday calendars and the solver time axis reach."""
    max_critical_paths: PositiveInt = 50
    """Guard against combinatorial growth when many equal-length critical chains exist."""


class Retention(BaseModel):
    """How long stored data is kept (ADR-010)."""

    model_config = _SETTINGS

    snapshots_days: PositiveInt = 365
    plans_days: PositiveInt = 30
    exports_days: PositiveInt = 7


class HttpCache(BaseModel):
    """HTTP cache lifetimes for configuration data (DESIGN §11.2)."""

    model_config = _SETTINGS

    configuration_ttl_s: PositiveInt = 3600
    sprints_ttl_s: PositiveInt = 600


class TokenBudgets(BaseModel):
    """Measured token budgets for MCP outputs (DESIGN §13.3)."""

    model_config = _SETTINGS

    summary: PositiveInt = 500
    full: PositiveInt = 1500
    tool_definitions: PositiveInt = 5000


class PoliteProfile(BaseModel):
    """Limits for polite-profile sites (DESIGN §9.8). Each only ever tightens the standard
    limit: lower rates and caps, longer intervals and cache lifetimes."""

    model_config = _SETTINGS

    requests_per_second: PositiveFloat = 1.0
    max_concurrent_requests_per_site: PositiveInt = 1
    max_open_issues: PositiveInt = 500
    reporting_window_days: PositiveInt = 30
    """Changelog history window."""
    full_reload_every: PositiveInt = 50
    refresh_interval_s: PositiveFloat = 900.0
    configuration_ttl_s: PositiveInt = 86_400
    retry_base_wait_s: PositiveFloat = 5.0
    max_consecutive_429: PositiveInt = 3


def default_data_dir() -> Path:
    """The per-user data folder (SQLite store, HTTP cache, exports), per OS conventions."""
    return platformdirs.user_data_path(APP_NAME, appauthor=False)


def default_config_file() -> Path:
    """``pulseboard.toml`` in the per-user configuration folder, per OS conventions."""
    return platformdirs.user_config_path(APP_NAME, appauthor=False) / DEFAULT_CONFIG_FILE


# ---------------------------------------------------------------- root


class Settings(BaseSettings):
    """All Pulseboard settings. Environment variables override the file:
    ``PULSEBOARD_<SECTION>__<FIELD>``, for example ``PULSEBOARD_TEAM__TIME_ZONE``.
    """

    model_config = SettingsConfigDict(
        env_prefix="PULSEBOARD_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    sites: tuple[Site, ...] = ()
    """No configured sites means no remote calls at all."""
    team: TeamSettings = TeamSettings()
    limits: Limits = Limits()
    retention: Retention = Retention()
    http_cache: HttpCache = HttpCache()
    token_budgets: TokenBudgets = TokenBudgets()
    polite: PoliteProfile = PoliteProfile()
    disabled_warnings: tuple[WarningId, ...] = ()
    data_dir: Path = Field(default_factory=default_data_dir)
    """SQLite snapshot store, HTTP cache and exports (local mode)."""

    @model_validator(mode="after")
    def _unique_sites(self) -> Self:
        ids = [s.site_id for s in self.sites]
        if len(ids) != len(set(ids)):
            msg = "Each site id may appear only once."
            raise ValueError(msg)
        origins = [str(s.origin) for s in self.sites]
        if len(origins) != len(set(origins)):
            msg = "Each base_url may appear only once."
            raise ValueError(msg)
        return self

    def for_site(self, site: Site) -> Self:
        """These settings with the limits of ``site``'s request profile applied (DESIGN §9.8)."""
        if site.profile is RequestProfile.STANDARD:
            return self
        polite, limits = self.polite, self.limits
        consecutive = polite.max_consecutive_429
        if limits.max_consecutive_429 is not None:
            consecutive = min(consecutive, limits.max_consecutive_429)
        tightened = limits.model_copy(
            update={
                "requests_per_second": min(limits.requests_per_second, polite.requests_per_second),
                "max_concurrent_requests_per_site": min(
                    limits.max_concurrent_requests_per_site,
                    polite.max_concurrent_requests_per_site,
                ),
                "max_open_issues": min(limits.max_open_issues, polite.max_open_issues),
                "reporting_window_days": min(
                    limits.reporting_window_days, polite.reporting_window_days
                ),
                "full_reload_every": max(limits.full_reload_every, polite.full_reload_every),
                "refresh_interval_s": max(limits.refresh_interval_s, polite.refresh_interval_s),
                "retry_base_wait_s": max(limits.retry_base_wait_s, polite.retry_base_wait_s),
                "max_consecutive_429": consecutive,
            }
        )
        cache = self.http_cache.model_copy(
            update={
                "configuration_ttl_s": max(
                    self.http_cache.configuration_ttl_s, polite.configuration_ttl_s
                )
            }
        )
        return self.model_copy(update={"limits": tightened, "http_cache": cache})

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: explicit arguments, then environment, then the TOML file."""
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


ENV_PREFIX = "PULSEBOARD_"
ENV_NESTING = "__"


def setting_variables(model: type[BaseModel] = Settings, prefix: str = ENV_PREFIX) -> set[str]:
    """Every environment variable that sets a setting: ``PULSEBOARD_<FIELD>`` and, for sections,
    ``PULSEBOARD_<SECTION>__<FIELD>`` (any depth)."""
    found: set[str] = set()
    for name, info in model.model_fields.items():
        variable = f"{prefix}{name.upper()}"
        found.add(variable)
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found |= setting_variables(annotation, f"{variable}{ENV_NESTING}")
    return found


def unknown_variables(environ: Mapping[str, str], known: Iterable[str]) -> tuple[str, ...]:
    """``PULSEBOARD_*`` variables that set nothing: a typo or an outdated name is otherwise
    ignored without a sign (ADDENDUM-03 §13). Only names are returned, never values."""
    expected = setting_variables() | set(known) | {CONFIG_PATH_ENV}
    return tuple(
        sorted(n for n in environ if n.upper().startswith(ENV_PREFIX) and n.upper() not in expected)
    )


class SettingsLocation(NamedTuple):
    """The settings file in use and why it was chosen (``pulseboard config path``)."""

    path: Path | None
    """``None`` when no settings file exists: Pulseboard runs with no sites."""
    reason: str


def locate_settings(
    cli_path: str | None, environ: Mapping[str, str] = os.environ
) -> SettingsLocation:
    """The settings file: ``--config``, then ``$PULSEBOARD_CONFIG``, then the per-user
    configuration folder; first match wins (DESIGN §9.9.1)."""
    if cli_path:
        return SettingsLocation(Path(cli_path), "--config option")
    if configured := environ.get(CONFIG_PATH_ENV):
        return SettingsLocation(Path(configured), f"{CONFIG_PATH_ENV} environment variable")
    default = default_config_file()
    if default.is_file():
        return SettingsLocation(default, "per-user configuration folder")
    return SettingsLocation(None, f"no settings file found (looked for {default})")


def _credential_paths(node: object, path: str = "") -> list[str]:
    """Dotted paths of credential-looking keys anywhere in a parsed TOML document."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).lower() in CREDENTIAL_KEYS:
                found.append(here)
            found.extend(_credential_paths(value, here))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_credential_paths(item, f"{path}[{index}]"))
    return found


def _location(loc: Sequence[int | str], document: Mapping[str, object]) -> str:
    """An error location, naming sites by their ID where the file gives one."""
    parts = [str(p) for p in loc]
    sites = document.get("sites")
    if len(loc) >= 2 and loc[0] == "sites" and isinstance(loc[1], int) and isinstance(sites, list):
        entry = sites[loc[1]] if loc[1] < len(sites) else None
        site_id = entry.get("id") if isinstance(entry, Mapping) else None
        if isinstance(site_id, str) and site_id.isprintable():
            parts = [f"sites[{site_id[:_SITE_ID_ECHO_MAX_CHARS]}]", *parts[2:]]
    return ".".join(parts) or "(root)"


def load_settings(path: Path | None) -> Settings:
    """Load and validate settings from ``path`` (or defaults only if ``None``).

    Raises:
        ConfigError: with every problem listed (naming the site and field), if the file is
            missing or invalid. Input values are never echoed, so a misplaced secret cannot leak.
    """
    if path is not None and not path.is_file():
        msg = f"Settings file not found: {path}. Run `pulseboard site add` to create it."
        raise ConfigError(msg)

    source = str(path) if path is not None else "environment"
    document: dict[str, object] = {}
    if path is not None:
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            msg = f"Settings file {source} is not valid TOML: {exc}"
            raise ConfigError(msg) from exc
        if found := _credential_paths(document):
            msg = (
                f"Invalid settings in {source}: credentials ({', '.join(found)}) must not be "
                f"in the settings file. {CREDENTIAL_HINT}"
            )
            raise ConfigError(msg)

    class _FileSettings(Settings):
        model_config = SettingsConfigDict(toml_file=path)

    try:
        return _FileSettings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {_location(err['loc'], document)}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        msg = f"Invalid settings in {source}:\n{problems}"
        raise ConfigError(msg) from exc
