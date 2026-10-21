"""Log redaction for HTTP mode (ADDENDUM-01 §1A.3).

Every log handler gets ``RedactingFilter``. It masks values whose names look like credentials:
``authorization``, ``token``, ``pat``, ``api_key``, ``password``, ``secret``, ``cert``, ``b64``,
``private_key``, and every ``x-jira-*`` and ``x-pulseboard-*`` header. It applies to mapping
arguments, ``extra`` fields and ``name: value`` / ``name=value`` text. Signed export paths
(``/exports/<token>``) are masked in every log, the access log included, because a link grants
access while it is valid. Uvicorn's access log has no headers.
"""

import logging
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

REDACTED = "[redacted]"
_SENSITIVE_WORDS = frozenset(
    {"authorization", "cookie", "token", "pat", "password", "secret", "cert", "certificate", "b64"}
)
_SENSITIVE_PHRASES = ("api_key", "apikey", "private_key")
_SENSITIVE_PREFIXES = ("x_jira_", "x_pulseboard_")
_NAME = (
    r"x-(?:jira|pulseboard)-[\w-]+|authorization|cookie|pat"
    r"|[\w-]*(?:token|password|passwd|secret|api[_-]?key|private[_-]?key|_b64)[\w-]*"
)
_VALUE = r"""(?:(?:basic|bearer)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;&}]+)"""
"""A quoted or bare value, after an optional auth scheme (``Authorization: Basic …``)."""
_PAIR = re.compile(rf"""(?i)\b({_NAME})(["']?\s*[:=]\s*)({_VALUE})""")
_SCHEME = re.compile(r"(?i)\b(basic|bearer)\s+[\w.~+/=-]+")
_EXPORT_LINK = re.compile(r"/exports/[\w.~-]+")
_STANDARD_FIELDS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_MAX_DEPTH = 8


def is_sensitive(name: str) -> bool:
    """Whether a field or header name holds a credential."""
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (
        any(part in _SENSITIVE_WORDS for part in normalized.split("_"))
        or any(phrase in normalized for phrase in _SENSITIVE_PHRASES)
        or f"{normalized}_".startswith(_SENSITIVE_PREFIXES)
    )


def redact_text(text: str) -> str:
    """``text`` with credential pairs, auth schemes and export links masked."""
    text = _PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _EXPORT_LINK.sub(f"/exports/{REDACTED}", text)


def redact(value: Any, depth: int = 0) -> Any:  # noqa: ANN401 - any loggable value
    """``value`` with sensitive mapping entries and text masked (nested up to a depth)."""
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive(str(key)) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return type(value)(redact(item, depth + 1) for item in value)
    return value


def redact_message(record: logging.LogRecord) -> None:
    """Mask the message of ``record`` in place.

    Arguments keep their shape (uvicorn's access formatter unpacks them, and the signed path is one
    of them). Only when a secret spans the format string and its arguments (``"token=%s"``) is the
    record collapsed to its masked text.
    """
    if isinstance(record.args, Mapping | tuple):
        record.args = redact(record.args)
    try:
        message = record.getMessage()
    except (TypeError, ValueError):  # a malformed record: keep only its masked text
        record.msg, record.args = redact_text(str(record.msg)), None
        return
    masked = redact_text(message)
    if masked != message:
        record.msg, record.args = masked, None


class RedactingFilter(logging.Filter):
    """Masks credentials in a record before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place; never drops it."""
        for name in set(vars(record)) - _STANDARD_FIELDS:
            value = getattr(record, name)
            setattr(record, name, REDACTED if is_sensitive(name) else redact(value))
        redact_message(record)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


class _RedactingFactory:
    """Wraps the log record factory, so every record is masked when it is created, whichever
    handler later emits it (including handlers added after start)."""

    def __init__(self, previous: Callable[..., logging.LogRecord]) -> None:
        self.previous = previous

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:  # noqa: ANN401
        record = self.previous(*args, **kwargs)
        redact_message(record)
        return record


def install_redaction() -> None:
    """Mask every record at creation, and add ``RedactingFilter`` (which also covers ``extra``
    fields) to every handler that exists now: root, uvicorn, FastMCP. Idempotent."""
    if not isinstance(logging.getLogRecordFactory(), _RedactingFactory):
        logging.setLogRecordFactory(_RedactingFactory(logging.getLogRecordFactory()))
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers += [
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())


def configure_logging(level: int = logging.INFO) -> None:
    """Root logging to standard error, redacted. Uvicorn is started without its own logging
    configuration, so its records reach these handlers too."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    install_redaction()
