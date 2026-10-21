"""Records and blocks network access in unit tests."""

from dataclasses import dataclass, field


class NetworkBlockedError(RuntimeError):
    """A unit test tried to reach the network."""


@dataclass
class NetworkGuard:
    """Records every blocked network attempt."""

    attempts: list[str] = field(default_factory=list)
