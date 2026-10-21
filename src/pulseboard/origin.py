"""URL splitting and origin normalization for configured Jira sites (DESIGN §9.1).

Uses only ``urllib.parse``; no regular-expression URL validation.
"""

from typing import Literal, NamedTuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

Scheme = Literal["https", "http"]

_DEFAULT_PORTS: dict[str, int] = {"https": 443, "http": 80}


class SplitUrl(NamedTuple):
    """A URL broken into the parts Pulseboard compares and reads."""

    scheme: Scheme
    host: str
    port: int | None
    path: str
    query: str


class Origin(BaseModel):
    """A normalized site origin: scheme, lowercase host, non-default port and context path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Scheme
    host: str
    port: int | None
    context_path: str
    """Empty, or a path such as ``/jira`` with no trailing slash."""

    def __str__(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{self.host}{port}{self.context_path}"

    def same_server(self, url: SplitUrl) -> bool:
        """Whether ``url`` has this origin's host and port (any scheme)."""
        return url.host == self.host and url.port == self.port

    def matches(self, url: SplitUrl) -> bool:
        """Whether ``url`` is on this exact origin and inside its context path."""
        return url.scheme == self.scheme and self.same_server(url) and self.owns_path(url.path)

    def owns_path(self, path: str) -> bool:
        """Whether ``path`` lies inside the context path, on a segment boundary."""
        if not self.context_path:
            return True
        return path == self.context_path or path.startswith(self.context_path + "/")


def split_url(url: str) -> SplitUrl:
    """Split and normalize ``url``.

    Raises:
        ValueError: if the URL is not an absolute HTTP(S) URL with a host, contains
            whitespace, control characters or credentials, or has an invalid port.
    """
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        msg = "The link contains whitespace or control characters."
        raise ValueError(msg)
    parts = urlsplit(url)
    scheme: Scheme
    match parts.scheme.lower():
        case "https":
            scheme = "https"
        case "http":
            scheme = "http"
        case _:
            msg = "The link must start with https://."
            raise ValueError(msg)
    if parts.username is not None or parts.password is not None:
        msg = "The link must not contain a user name or password."
        raise ValueError(msg)
    host = (parts.hostname or "").rstrip(".")
    if not host:
        msg = "The link has no host."
        raise ValueError(msg)
    port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    return SplitUrl(scheme=scheme, host=host, port=port, path=parts.path, query=parts.query)


def origin_from_base_url(url: str) -> Origin:
    """Normalize a configured site base URL into an ``Origin``.

    Raises:
        ValueError: if ``url`` is not a valid base URL (see ``split_url``), or has a
            query string or fragment.
    """
    split = split_url(url)
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        msg = "A site base URL must not have a query string or fragment."
        raise ValueError(msg)
    return Origin(
        scheme=split.scheme,
        host=split.host,
        port=split.port,
        context_path=split.path.rstrip("/"),
    )
