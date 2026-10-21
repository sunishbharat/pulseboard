"""Exception hierarchy and the typed error codes returned at the MCP edge (DESIGN §12.5)."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Typed error codes an AI client can act on."""

    SITE_NOT_CONFIGURED = "site_not_configured"
    INVALID_BOARD_LINK = "invalid_board_link"
    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    BOARD_NOT_FOUND = "board_not_found"
    RATE_LIMITED = "rate_limited"
    BOARD_TOO_LARGE = "board_too_large"
    PLAN_EXPIRED = "plan_expired"
    JIRA_UNAVAILABLE = "jira_unavailable"
    TIMEOUT = "timeout"
    SERVER_BUSY = "server_busy"
    NOT_AVAILABLE = "not_available"


DURABLE_STORAGE_MESSAGE = (
    "Falling back to an older plan requires durable storage, which this hosted server "
    "(Profile A) does not have."
)


class PulseboardError(Exception):
    """Base class for every error Pulseboard raises on purpose.

    Messages say what happened and what to do next. They never contain credentials.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(PulseboardError):
    """The settings file or environment is invalid. Raised at start, before any work."""


class ToolError(PulseboardError):
    """An error that maps to a typed error code at the MCP edge."""

    code: ErrorCode

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class SiteNotConfiguredError(ToolError):
    """The board link's origin does not match any configured Jira site."""

    code = ErrorCode.SITE_NOT_CONFIGURED


class InvalidBoardLinkError(ToolError):
    """The board link is malformed or has no board ID."""

    code = ErrorCode.INVALID_BOARD_LINK


class AuthFailedError(ToolError):
    """Credentials are missing or Jira rejected them."""

    code = ErrorCode.AUTH_FAILED


class PermissionDeniedError(ToolError):
    """The Jira user cannot see this board or resource."""

    code = ErrorCode.PERMISSION_DENIED


class BoardNotFoundError(ToolError):
    """Jira has no such board (or resource)."""

    code = ErrorCode.BOARD_NOT_FOUND


class RateLimitedError(ToolError):
    """Jira throttled the request and retries were exhausted."""

    code = ErrorCode.RATE_LIMITED

    def __init__(
        self, message: str, *, retry_after_s: float | None, hint: str | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.retry_after_s = retry_after_s


class BoardTooLargeError(ToolError):
    """The board has more open issues than the configured limit."""

    code = ErrorCode.BOARD_TOO_LARGE


class PlanExpiredError(ToolError):
    """The plan ID is unknown or was purged."""

    code = ErrorCode.PLAN_EXPIRED


class JiraUnavailableError(ToolError):
    """Jira is unreachable and no recent plan can be served."""

    code = ErrorCode.JIRA_UNAVAILABLE


class ToolTimeoutError(ToolError):
    """The tool exceeded its time budget."""

    code = ErrorCode.TIMEOUT


class ServerBusyError(ToolError):
    """Every solver slot stayed taken for the whole tool timeout (hosted, ADDENDUM-01 §1A.6)."""

    code = ErrorCode.SERVER_BUSY

    def __init__(self) -> None:
        super().__init__("The server is busy planning other boards.", hint="Retry shortly.")


class NotAvailableError(ToolError):
    """The feature exists but this server does not offer it (hosted Profile A)."""

    code = ErrorCode.NOT_AVAILABLE


class DurableStorageRequiredError(NotAvailableError):
    """The feature needs durable storage, which hosted Profile A does not have (DESIGN §12.2):
    baseline variance, plan changes across restarts, automatic baselines, stale plans."""

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"{feature} requires durable storage, which this hosted server does not have.",
            hint="Run Pulseboard locally (stdio) for this feature.",
        )
