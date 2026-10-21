"""Solver slots (ADDENDUM-01 §1A.6): abandoned solves keep their slot until they really end."""

import threading

import anyio
import pytest

from pulseboard.services.solve_slots import SolveSlots


async def wait_until_free(slots: SolveSlots) -> None:
    with anyio.fail_after(10):
        while slots.in_use:
            await anyio.sleep(0.01)


async def test_a_slot_is_released_after_the_work() -> None:
    slots = SolveSlots(1)

    async with slots.reserve() as slot:
        assert slots.in_use == 1
        assert await slot.run(lambda: 42) == 42

    assert slots.in_use == 0


async def test_an_abandoned_solve_keeps_its_slot_until_it_ends() -> None:
    slots, gate = SolveSlots(1), threading.Event()

    with anyio.move_on_after(0.1) as scope:
        async with slots.reserve() as slot:
            await slot.run(lambda: gate.wait(10))

    assert scope.cancelled_caught
    assert slots.in_use == 1  # the worker thread still runs
    with anyio.move_on_after(0.1) as waiting:
        async with slots.reserve():
            pytest.fail("a second solve started while the first still ran")
    assert waiting.cancelled_caught
    gate.set()
    await wait_until_free(slots)


async def test_a_reservation_left_before_the_work_is_released() -> None:
    slots = SolveSlots(1)

    async with slots.reserve():
        pass

    assert slots.in_use == 0


async def test_failing_work_releases_its_slot() -> None:
    slots = SolveSlots(1)

    def fail() -> None:
        msg = "solver crashed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="solver crashed"):
        async with slots.reserve() as slot:
            await slot.run(fail)
    await wait_until_free(slots)


async def test_two_slots_run_two_solves_at_once() -> None:
    slots, both, gate = SolveSlots(2), threading.Barrier(2, timeout=5), threading.Event()

    def work() -> None:
        both.wait()  # only returns if both solves run at the same time
        gate.wait(5)

    async def solve() -> None:
        async with slots.reserve() as slot:
            await slot.run(work)

    async with anyio.create_task_group() as group:
        group.start_soon(solve)
        group.start_soon(solve)
        with anyio.fail_after(5):
            while slots.in_use < 2:
                await anyio.sleep(0.01)
        gate.set()

    assert slots.total == 2
    assert slots.in_use == 0
