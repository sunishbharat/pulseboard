"""Monte Carlo forecast from weekly throughput (DESIGN §10.7) and the confidence policy (§3.1).

Each trial samples weekly throughput with replacement until the remaining items are done. The
generator is seeded (the seed is derived from ``plan_id`` by the caller), so results reproduce.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt

from pulseboard.domain.plan import Forecast
from pulseboard.domain.snapshot import Task
from pulseboard.domain.types import Confidence, ForecastMethod, StatusCategory
from pulseboard.engine.calendar import WorkCalendar, local_date

DAYS_PER_WEEK = 7
WORKING_DAYS_PER_WEEK = 5
PERCENTILES = (50, 85, 95)
_CHUNK_WEEKS = 52
_MAX_WEEKS = 520
"""Trials still unfinished after ten years are counted at this limit."""


@dataclass(frozen=True)
class Throughput:
    """Done items per week, oldest week first, over the weeks the board has existed."""

    weekly: tuple[int, ...]

    @property
    def weeks(self) -> int:
        """Weeks of history."""
        return len(self.weekly)


def weekly_throughput(
    tasks: Sequence[Task], plan_date: date, time_zone: str, history_weeks: int
) -> Throughput:
    """Count resolutions per week in the ``history_weeks`` weeks up to the plan date.

    Week 0 is the seven days ending on the plan date. Weeks before the oldest task was created
    are left out, so a young board is not padded with zero weeks.
    """
    if not tasks:
        return Throughput(())
    oldest = min(local_date(t.created, time_zone) for t in tasks)
    available = (plan_date - oldest).days // DAYS_PER_WEEK + 1
    weeks = max(min(history_weeks, available), 0)
    counts = [0] * weeks
    for task in tasks:
        if task.status_category is not StatusCategory.DONE or task.resolved is None:
            continue
        age = (plan_date - local_date(task.resolved, time_zone)).days
        index = age // DAYS_PER_WEEK
        if age >= 0 and index < weeks:
            counts[weeks - 1 - index] += 1
    return Throughput(tuple(counts))


_SEED_HEX_DIGITS = 8


def seed_for(plan_id: str) -> int:
    """The Monte Carlo seed derived from the plan ID, so results reproduce (DESIGN §10.7)."""
    return int(plan_id[:_SEED_HEX_DIGITS], 16)


def simulate_trials(
    remaining: int, weekly: Sequence[int], trials: int, seed: int
) -> npt.NDArray[np.int64] | None:
    """Weeks each trial needs to finish ``remaining`` items; ``None`` if throughput is all zero."""
    if remaining <= 0:
        return np.zeros(trials, dtype=np.int64)
    history = np.asarray(weekly, dtype=np.int64)
    if history.size == 0 or history.sum() == 0:
        return None
    rng = np.random.default_rng(seed)
    done = np.zeros(trials, dtype=np.int64)
    finished_at = np.full(trials, _MAX_WEEKS, dtype=np.int64)
    pending = np.ones(trials, dtype=bool)
    for offset in range(0, _MAX_WEEKS, _CHUNK_WEEKS):
        samples = rng.choice(history, size=(trials, _CHUNK_WEEKS))
        totals = done[:, None] + samples.cumsum(axis=1)
        reached = totals >= remaining
        hit = pending & reached.any(axis=1)
        finished_at[hit] = offset + reached[hit].argmax(axis=1) + 1
        pending &= ~hit
        done = totals[:, -1]
        if not pending.any():
            break
    return finished_at


def simulate_weeks(
    remaining: int, weekly: Sequence[int], trials: int, seed: int
) -> tuple[int, int, int] | None:
    """P50, P85 and P95 weeks to finish ``remaining`` items; ``None`` if throughput is all zero."""
    finished_at = simulate_trials(remaining, weekly, trials, seed)
    if finished_at is None:
        return None
    p50, p85, p95 = np.percentile(finished_at, PERCENTILES, method="higher")
    return int(p50), int(p85), int(p95)


def weeks_to_date(weeks: int, calendar: WorkCalendar) -> date:
    """The working day on which ``weeks`` weeks of work end (the origin for zero weeks)."""
    if weeks <= 0:
        return calendar.origin
    return calendar.add_working_days(calendar.origin, weeks * WORKING_DAYS_PER_WEEK - 1)


def monte_carlo_forecast(
    remaining: int,
    throughput: Throughput,
    calendar: WorkCalendar,
    *,
    trials: int,
    seed: int,
    min_history_weeks: int,
) -> Forecast | None:
    """The Monte Carlo forecast, or ``None`` when there is no throughput to sample."""
    weeks = simulate_weeks(remaining, throughput.weekly, trials, seed)
    if weeks is None:
        return None
    if throughput.weeks < min_history_weeks:
        confidence = Confidence.LOW
        reason = f"only {throughput.weeks} weeks of throughput history"
    else:
        confidence = Confidence.MEDIUM
        reason = f"{throughput.weeks} weeks of throughput history"
    p50, p85, p95 = (weeks_to_date(w, calendar) for w in weeks)
    return Forecast(
        method=ForecastMethod.MONTE_CARLO,
        p50=p50,
        p85=p85,
        p95=p95,
        trial_count=trials,
        seed=seed,
        history_weeks=throughput.weeks or None,
        confidence=confidence,
        confidence_reason=reason,
    )


def choose_primary(
    schedule: Forecast, monte_carlo: Forecast | None, share: float, threshold: float
) -> tuple[Forecast, Forecast | None]:
    """(primary, secondary) per DESIGN §3.1.

    At least ``threshold`` estimated: the schedule is primary. Otherwise Monte Carlo is primary,
    falling back to the schedule when there is no throughput history to sample.
    """
    if share >= threshold or monte_carlo is None:
        return schedule, monte_carlo
    return monte_carlo, schedule
