"""A cap on concurrent solves (ADDENDUM-01 §1A.6: 1–2 per hosted instance).

A slot stays taken until the solver thread really ends. When a caller's tool timeout passes, its
worker thread is abandoned but keeps the CPU busy, so the slot must stay taken too. anyio's own
thread limiter is released on abandonment, which is why this is not simply ``limiter=``.
"""

import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from enum import Enum, auto

import anyio
import anyio.from_thread
import anyio.to_thread


class _State(Enum):
    RESERVED = auto()
    RUNNING = auto()
    RELEASED = auto()


class SlotReleasedError(RuntimeError):
    """The caller left before its solve started; the solve is skipped."""


class Slot:
    """One reserved solver slot. ``run`` hands it to a worker thread, which releases it."""

    def __init__(self, limiter: anyio.CapacityLimiter, borrower: object) -> None:
        self._limiter = limiter
        self._borrower = borrower
        self._lock = threading.Lock()
        self._state = _State.RESERVED

    async def run[T](self, work: Callable[[], T]) -> T:
        """Run ``work`` in a worker thread that keeps this slot until it returns."""

        def hold_slot_while_working() -> T:
            with self._lock:
                if self._state is not _State.RESERVED:
                    raise SlotReleasedError
                self._state = _State.RUNNING
            try:
                return work()
            finally:
                with self._lock:
                    self._state = _State.RELEASED
                # RuntimeError: the event loop is gone (shutdown), so there is nothing to release.
                with suppress(RuntimeError):
                    anyio.from_thread.run_sync(self._limiter.release_on_behalf_of, self._borrower)

        return await anyio.to_thread.run_sync(hold_slot_while_working, abandon_on_cancel=True)

    def release_if_unused(self) -> None:
        """Release the slot unless a worker thread holds it (that thread releases it)."""
        with self._lock:
            if self._state is not _State.RESERVED:
                return
            self._state = _State.RELEASED
        self._limiter.release_on_behalf_of(self._borrower)


class SolveSlots:
    """At most ``total`` solves at a time."""

    def __init__(self, total: int) -> None:
        self._limiter = anyio.CapacityLimiter(total)

    @property
    def total(self) -> int:
        """The number of slots."""
        return int(self._limiter.total_tokens)

    @property
    def in_use(self) -> int:
        """Slots taken now, including by abandoned solves still running."""
        return self._limiter.borrowed_tokens

    @asynccontextmanager
    async def reserve(self) -> AsyncIterator[Slot]:
        """Wait for a free slot (cancellable, e.g. by the tool timeout)."""
        borrower = object()
        await self._limiter.acquire_on_behalf_of(borrower)
        slot = Slot(self._limiter, borrower)
        try:
            yield slot
        finally:
            slot.release_if_unused()
