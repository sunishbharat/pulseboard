"""WorkCalendar (DESIGN §10.1; §18.1 #5, #6, #8)."""

from datetime import UTC, date, datetime

import pytest

from pulseboard.domain.types import AccountId
from pulseboard.engine.calendar import PersonCalendar, WorkCalendar, local_date

ALICE = AccountId("alice")
BOB = AccountId("bob")


def calendar(
    plan_date: date = date(2026, 10, 19),
    *,
    people: tuple[PersonCalendar, ...] = (),
    extra_non_working_dates: tuple[date, ...] = (),
) -> WorkCalendar:
    return WorkCalendar(
        plan_date=plan_date,
        team_holiday_country="US",
        hours_per_day=8.0,
        horizon_years=1,
        people=people,
        extra_non_working_dates=extra_non_working_dates,
    )


def test_origin_rolls_forward_from_weekend() -> None:
    cal = calendar(date(2026, 10, 17))  # Saturday

    assert cal.origin == date(2026, 10, 19)
    assert cal.unit_of_date(date(2026, 10, 17)) == 0


def test_weekend_is_skipped() -> None:
    """§18.1 #5: Friday is unit 8-9; the next working unit (10) is Monday."""
    cal = calendar()

    assert cal.date_of_unit(9) == date(2026, 10, 23)
    assert cal.date_of_unit(10) == date(2026, 10, 26)
    assert cal.end_date(10) == date(2026, 10, 23)
    assert not cal.is_working_day(date(2026, 10, 24))


def test_public_holiday_pushes_dates() -> None:
    """§18.1 #6: US Thanksgiving 2026-11-26 is not on the axis."""
    cal = calendar(date(2026, 11, 23))

    assert cal.date_of_unit(6) == date(2026, 11, 27)  # Mon, Tue, Wed, (Thu off), Fri
    assert cal.working_days_between(date(2026, 11, 23), date(2026, 11, 30)) == 4.0


def test_extra_non_working_dates() -> None:
    cal = calendar(extra_non_working_dates=(date(2026, 10, 20),))

    assert cal.date_of_unit(2) == date(2026, 10, 21)


def test_half_capacity_doubles_duration() -> None:
    """§18.1 #8."""
    cal = calendar(people=(PersonCalendar(BOB, capacity=0.5, holiday_country="US"),))

    assert cal.duration_units(16.0, ALICE) == 4
    assert cal.duration_units(16.0, BOB) == 8
    assert cal.duration_units(16.0, None) == 4


@pytest.mark.parametrize(("hours", "units"), [(0.0, 1), (1.0, 1), (4.0, 1), (4.1, 2), (12.0, 3)])
def test_duration_rounds_up_to_half_days(hours: float, units: int) -> None:
    assert calendar().duration_units(hours, ALICE) == units


def test_leave_becomes_breaks() -> None:
    leave = ((date(2026, 10, 21), date(2026, 10, 27)),)  # Wed..Tue, spans a weekend
    cal = calendar(people=(PersonCalendar(ALICE, 1.0, "US", leave),))

    assert cal.breaks(ALICE) == ((4, 14),)  # Wed, Thu, Fri, Mon, Tue
    assert cal.breaks(BOB) == ()


def test_own_holiday_country_becomes_breaks() -> None:
    """A German team member is off on German Unity Day while the US team works."""
    cal = calendar(date(2028, 10, 2), people=(PersonCalendar(ALICE, 1.0, "DE"),))

    assert (2, 4) in cal.breaks(ALICE)  # Tuesday 2028-10-03
    assert cal.date_of_unit(2) == date(2028, 10, 3)


def test_working_days_helpers() -> None:
    cal = calendar()

    assert cal.working_days_between(date(2026, 10, 23), date(2026, 10, 19)) == -4.0
    assert cal.working_days_inclusive(date(2026, 10, 19), date(2026, 10, 25)) == 5
    assert cal.working_days_inclusive(date(2026, 10, 25), date(2026, 10, 19)) == 0
    assert cal.add_working_days(date(2026, 10, 23), 1) == date(2026, 10, 26)
    assert cal.units_to_working_days(5) == 2.5
    assert cal.horizon_units == 2 * cal.working_days_between(cal.origin, date(2028, 1, 1))


def test_local_date_uses_team_time_zone() -> None:
    instant = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)

    assert local_date(instant, "Asia/Tokyo") == date(2026, 10, 21)
    assert local_date(instant, "America/New_York") == date(2026, 10, 20)
