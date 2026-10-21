"""IANA time zone names, validated against the system or ``tzdata`` database."""

from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator


def _check_time_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"'{name}' is not an IANA time zone name, such as 'Europe/Berlin'."
        raise ValueError(msg) from exc
    return name


TimeZoneName = Annotated[str, AfterValidator(_check_time_zone)]
"""An IANA time zone name such as ``Europe/Berlin``."""
