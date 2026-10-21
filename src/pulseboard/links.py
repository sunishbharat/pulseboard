"""Board link parsing, bound to the site registry (DESIGN §9.1).

Accepted link shapes (after the site's context path):

- ``/jira/software/projects/KEY/boards/42`` (with optional ``/c/`` and trailing views
  such as ``/backlog`` or ``/timeline``)
- any path containing ``/boards/42``
- ``/secure/RapidBoard.jspa?rapidView=42[&projectKey=KEY]`` (Data Center and older Cloud)

Uses ``urllib.parse`` only.
"""

from urllib.parse import parse_qs

from pulseboard.domain.snapshot import BoardRef
from pulseboard.domain.types import BoardId
from pulseboard.errors import InvalidBoardLinkError
from pulseboard.sites import SiteRegistry

_BOARD_HINT = "Ask the user for the board's link from the browser address bar."
_MAX_BOARD_ID = 2**63 - 1
_RAPID_BOARD_PAGE = "RapidBoard.jspa"


def _board_id(raw: str | None) -> BoardId:
    if raw is None or not (raw.isascii() and raw.isdigit()):
        msg = "The link has no board ID."
        raise InvalidBoardLinkError(msg, hint=_BOARD_HINT)
    value = int(raw)
    if not 0 < value <= _MAX_BOARD_ID:
        msg = "The link's board ID is out of range."
        raise InvalidBoardLinkError(msg, hint=_BOARD_HINT)
    return BoardId(value)


def _project_key(raw: str | None) -> str | None:
    """A plausible Jira project key, upper-cased, or ``None``."""
    if not raw or not raw.isascii():
        return None
    key = raw.upper()
    if key[0].isalpha() and all(ch.isalnum() or ch == "_" for ch in key):
        return key
    return None


def _segment_after(segments: list[str], name: str) -> str | None:
    if name in segments:
        index = segments.index(name)
        if index + 1 < len(segments):
            return segments[index + 1]
    return None


def parse_board_link(url: str, registry: SiteRegistry) -> BoardRef:
    """Parse a Jira board link on a configured site.

    Raises:
        SiteNotConfiguredError: if the link's origin is not a configured site. No network
            call is made.
        InvalidBoardLinkError: if the link is malformed or has no board ID.
    """
    site, split = registry.match(url)
    relative = split.path[len(site.origin.context_path) :]
    segments = [segment for segment in relative.split("/") if segment]
    query = parse_qs(split.query)

    if segments and segments[-1] == _RAPID_BOARD_PAGE:
        board = _board_id(query.get("rapidView", [None])[0])
        project = _project_key(query.get("projectKey", [None])[0])
    else:
        board = _board_id(_segment_after(segments, "boards"))
        project = _project_key(_segment_after(segments, "projects"))
    return BoardRef(site_id=site.site_id, board_id=board, project_key=project)
