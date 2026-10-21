"""WorkCalendar: working time per person, on a typed seam over numpy business days (DESIGN §10.1).

The solver time axis counts **team working half-days** from the plan date: unit ``2·i`` is the
morning and ``2·i + 1`` the afternoon of the ``i``-th team working day on or after the plan
date. Weekends, the team's public holidays and extra non-working dates are not on the axis.
A person's own non-working days that the team works (leave, or public holidays of a different
holiday country) become **breaks** on that person's resource (ADR-019).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import holidays
import numpy as np
import numpy.typing as npt

from pulseboard.domain.types import AccountId, WorkingDays
from pulseboard.settings import TeamSettings

HALF_DAYS_PER_DAY = 2
_HISTORY_YEARS = 2
"""Holiday years kept before the plan date, for velocity and flow history."""
_EPSILON = 1e-9

Break = tuple[int, int]
"""A half-open interval ``[start, end)`` of time-axis units."""

DateArray = npt.NDArray[np.datetime64]


def _to_date(day: np.datetime64) -> date:
    return date.fromisoformat(str(day))


def local_date(instant: datetime, time_zone: str) -> date:
    """The calendar date of ``instant`` in ``time_zone``."""
    return instant.astimezone(ZoneInfo(time_zone)).date()


@dataclass(frozen=True)
class PersonCalendar:
    """One person's working pattern."""

    account_id: AccountId
    capacity: float
    """Share of full time, 0 < capacity <= 1."""
    holiday_country: str
    leave: tuple[tuple[date, date], ...] = ()
    """Inclusive leave periods."""


def _holiday_dates(country: str, years: Iterable[int]) -> list[date]:
    return sorted(holidays.country_holidays(country, years=list(years)).keys())


def _as_days(dates: Iterable[date]) -> DateArray:
    return np.array(sorted(set(dates)), dtype="datetime64[D]")


def _runs(indices: npt.NDArray[np.int64]) -> tuple[Break, ...]:
    """Merge sorted, unique day indices into half-day unit intervals."""
    if indices.size == 0:
        return ()
    starts = np.r_[True, np.diff(indices) != 1]
    ends = np.r_[np.diff(indices) != 1, True]
    return tuple(
        (int(a) * HALF_DAYS_PER_DAY, (int(b) + 1) * HALF_DAYS_PER_DAY)
        for a, b in zip(indices[starts], indices[ends], strict=True)
    )


class WorkCalendar:
    """Working time for a team and its people, from a fixed plan date."""

    def __init__(
        self,
        *,
        plan_date: date,
        team_holiday_country: str,
        hours_per_day: float,
        horizon_years: int,
        extra_non_working_dates: Sequence[date] = (),
        people: Sequence[PersonCalendar] = (),
        default_capacity: float = 1.0,
    ) -> None:
        years = range(plan_date.year - _HISTORY_YEARS, plan_date.year + horizon_years + 1)
        self._team_country = team_holiday_country
        self._years = years
        self._holidays = _as_days(
            [*_holiday_dates(team_holiday_country, years), *extra_non_working_dates]
        )
        self._holiday_names: dict[date, str] = dict(
            holidays.country_holidays(team_holiday_country, years=list(years)).items()
        )
        self._hours_per_day = hours_per_day
        self._default_capacity = default_capacity
        self._people = {p.account_id: p for p in people}
        self._origin = np.busday_offset(
            np.datetime64(plan_date, "D"), 0, roll="forward", holidays=self._holidays
        )
        self._horizon_end = np.datetime64(date(plan_date.year + horizon_years + 1, 1, 1), "D")
        self._breaks: dict[AccountId, tuple[Break, ...]] = {}

    # ------------------------------------------------------------ axis

    @property
    def origin(self) -> date:
        """The first team working day on or after the plan date (unit 0)."""
        return _to_date(self._origin)

    @property
    def horizon_units(self) -> int:
        """Number of units on the axis, up to the end of the planning horizon."""
        days = np.busday_count(self._origin, self._horizon_end, holidays=self._holidays)
        return int(days) * HALF_DAYS_PER_DAY

    def date_of_unit(self, unit: int) -> date:
        """The calendar date of time-axis ``unit``."""
        day = np.busday_offset(self._origin, unit // HALF_DAYS_PER_DAY, holidays=self._holidays)
        return _to_date(day)

    def end_date(self, end_unit: int) -> date:
        """The last date worked by a task ending (exclusively) at ``end_unit``."""
        return self.date_of_unit(max(end_unit - 1, 0))

    def unit_of_date(self, day: date) -> int:
        """The first unit on or after ``day``; 0 for days before the origin."""
        count = np.busday_count(self._origin, np.datetime64(day, "D"), holidays=self._holidays)
        return max(int(count), 0) * HALF_DAYS_PER_DAY

    def is_working_day(self, day: date) -> bool:
        """Whether ``day`` is a team working day."""
        return bool(np.is_busday(np.datetime64(day, "D"), holidays=self._holidays))

    def holiday_name(self, day: date) -> str | None:
        """The team's public holiday on ``day``, if any."""
        return self._holiday_names.get(day)

    def working_days_between(self, start: date, end: date) -> WorkingDays:
        """Team working days in ``[start, end)``; negative when ``end`` is before ``start``."""
        count = np.busday_count(
            np.datetime64(start, "D"), np.datetime64(end, "D"), holidays=self._holidays
        )
        return WorkingDays(float(count))

    def working_days_inclusive(self, start: date, end: date) -> int:
        """Team working days from ``start`` to ``end``, both included (0 if reversed)."""
        last = np.datetime64(end, "D") + np.timedelta64(1, "D")
        count = np.busday_count(np.datetime64(start, "D"), last, holidays=self._holidays)
        return max(int(count), 0)

    def add_working_days(self, day: date, count: int) -> date:
        """The team working day ``count`` working days after ``day`` (rolled forward first)."""
        result = np.busday_offset(
            np.datetime64(day, "D"), count, roll="forward", holidays=self._holidays
        )
        return _to_date(result)

    # ------------------------------------------------------------ people

    def person(self, account_id: AccountId) -> PersonCalendar:
        """The person's calendar, or team defaults for someone not configured."""
        return self._people.get(
            account_id,
            PersonCalendar(account_id, self._default_capacity, self._team_country),
        )

    def breaks(self, account_id: AccountId) -> tuple[Break, ...]:
        """Units the team works but this person does not (leave, own public holidays)."""
        if account_id not in self._breaks:
            self._breaks[account_id] = self._compute_breaks(self.person(account_id))
        return self._breaks[account_id]

    def _compute_breaks(self, person: PersonCalendar) -> tuple[Break, ...]:
        days: list[DateArray] = []
        if person.holiday_country != self._team_country:
            days.append(_as_days(_holiday_dates(person.holiday_country, self._years)))
        for start, end in person.leave:
            days.append(
                np.arange(
                    np.datetime64(start, "D"),
                    np.datetime64(end, "D") + np.timedelta64(1, "D"),
                    dtype="datetime64[D]",
                )
            )
        if not days:
            return ()
        candidates = np.unique(np.concatenate(days))
        in_range = candidates[(candidates >= self._origin) & (candidates < self._horizon_end)]
        working = in_range[np.is_busday(in_range, holidays=self._holidays)]
        indices = np.busday_count(self._origin, working, holidays=self._holidays).astype(np.int64)
        return _runs(indices)

    # ------------------------------------------------------------ effort

    def duration_units(self, effort_hours: float, account_id: AccountId | None) -> int:
        """Units needed for ``effort_hours`` of work at the person's capacity (at least 1)."""
        capacity = self.person(account_id).capacity if account_id else self._default_capacity
        hours_per_unit = self._hours_per_day / HALF_DAYS_PER_DAY
        return max(math.ceil(effort_hours / hours_per_unit / capacity - _EPSILON), 1)

    def units_to_working_days(self, units: int) -> WorkingDays:
        """Convert axis units to working days."""
        return WorkingDays(units / HALF_DAYS_PER_DAY)


def calendar_from_settings(plan_date: date, team: TeamSettings, horizon_years: int) -> WorkCalendar:
    """Build the team calendar from settings: holiday countries, capacity, leave."""
    configured = {p.account_id: p for p in team.people}
    leave: dict[AccountId, list[tuple[date, date]]] = {}
    for period in team.leave:
        leave.setdefault(period.account_id, []).append((period.start, period.end))
    people = []
    for account in sorted(set(configured) | set(leave)):
        settings = configured.get(account)
        people.append(
            PersonCalendar(
                account_id=account,
                capacity=(settings.capacity if settings and settings.capacity else None)
                or team.default_capacity,
                holiday_country=(settings.holiday_country if settings else None)
                or team.default_holiday_country,
                leave=tuple(sorted(leave.get(account, ()))),
            )
        )
    return WorkCalendar(
        plan_date=plan_date,
        team_holiday_country=team.default_holiday_country,
        hours_per_day=team.hours_per_day,
        horizon_years=horizon_years,
        extra_non_working_dates=team.extra_non_working_dates,
        people=people,
        default_capacity=team.default_capacity,
    )
