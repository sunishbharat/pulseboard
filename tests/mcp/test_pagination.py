"""Generic Page[T] paginator and token-budget guard (DESIGN §12.1, §13.3)."""

import pytest
from pydantic import BaseModel

from pulseboard.mcp.pagination import (
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    estimate_tokens,
    paginate,
)


class Row(BaseModel):
    n: int
    text: str = ""


ROWS = [Row(n=i) for i in range(45)]


def test_pages_walk_the_whole_list() -> None:
    seen: list[int] = []
    cursor: str | None = None
    while True:
        page = paginate(ROWS, cursor, page_size=20, budget_tokens=10_000)
        seen += [r.n for r in page.items]
        assert page.total == 45
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == list(range(45))


def test_cursor_round_trip_and_validation() -> None:
    assert decode_cursor(encode_cursor(20)) == 20
    assert decode_cursor(None) == 0
    for bad in ("not-base64!!", encode_cursor(1)[:-2] + "zz", "djE6YWJj"):  # last: "v1:abc"
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad)


def test_budget_shrinks_the_page() -> None:
    wide = [Row(n=i, text="x" * 300) for i in range(20)]
    page = paginate(wide, None, page_size=20, budget_tokens=500)

    assert 1 <= len(page.items) < 20
    assert estimate_tokens(page.model_dump_json()) <= 500
    assert page.next_cursor is not None


def test_a_single_oversized_item_is_still_returned() -> None:
    page = paginate([Row(n=1, text="y" * 5000)], None, page_size=20, budget_tokens=10)

    assert len(page.items) == 1


def test_cursor_past_the_end() -> None:
    page = paginate(ROWS, encode_cursor(100), page_size=20, budget_tokens=10_000)

    assert page.items == []
    assert page.next_cursor is None


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2
