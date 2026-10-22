"""The generic ``Page[T]`` cursor paginator and token-budget guard (DESIGN §12.1, §13.3).

Cursors are opaque to clients. Each page also respects a token budget: if the default page size
would exceed it, the page shrinks until it fits (at least one item is always returned).
"""

import base64
import math
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

CHARS_PER_TOKEN = 3
"""Conservative token estimate for JSON (real tokenizers average 3–4 characters per token)."""
_CURSOR_PREFIX = "v1:"
_OFFSET = re.compile(r"\d{1,9}", re.ASCII)


class InvalidCursorError(ValueError):
    """The cursor was not produced by this server."""


def estimate_tokens(text: str) -> int:
    """Estimated token count of ``text``."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class Page[T](BaseModel):
    """One page of a list result."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = None
    total: int | None = None
    note: str | None = None
    """Why the list is empty (ADDENDUM-03 §10)."""


def encode_cursor(offset: int) -> str:
    """An opaque cursor for ``offset``."""
    return base64.urlsafe_b64encode(f"{_CURSOR_PREFIX}{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """The offset a cursor points to (0 for no cursor).

    Raises:
        InvalidCursorError: for a cursor this server did not issue.
    """
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(cursor) from exc
    offset = text[len(_CURSOR_PREFIX) :]
    if not text.startswith(_CURSOR_PREFIX) or not _OFFSET.fullmatch(offset):
        raise InvalidCursorError(cursor)
    return int(offset)


def paginate[T: BaseModel](
    items: Sequence[T],
    cursor: str | None,
    page_size: int,
    budget_tokens: int,
    empty_note: str | None = None,
) -> Page[T]:
    """The page starting at ``cursor``, at most ``page_size`` items and within the budget.
    ``empty_note`` says why, when there are no items at all."""
    start = min(decode_cursor(cursor), len(items))
    size = max(min(page_size, len(items) - start), 0)
    while True:
        chunk = list(items[start : start + size])
        end = start + len(chunk)
        page = Page[T](
            items=chunk,
            next_cursor=encode_cursor(end) if end < len(items) else None,
            total=len(items),
            note=empty_note if not items else None,
        )
        if size <= 1 or estimate_tokens(page.model_dump_json()) <= budget_tokens:
            return page
        size -= 1
