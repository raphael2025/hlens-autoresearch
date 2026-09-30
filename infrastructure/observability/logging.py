"""Stdlib-only JSON logging for service output.

The formatter deliberately emits an allowlist of fields. In particular, arbitrary
``LogRecord`` extras and exception messages / tracebacks are not serialized.
Applications remain responsible for keeping sensitive values out of log messages.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

_HANDLER_MARKER = "_hlens_json_logging_handler"


class JSONFormatter(logging.Formatter):
    """Format each record as one compact JSON object with a fixed field set."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            # A broken __str__ or %-format argument must not break the caller's
            # logging path or cause arbitrary argument representations to leak.
            message = "<unformattable log message>"

        try:
            timestamp = (
                datetime.fromtimestamp(record.created, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            timestamp = "1970-01-01T00:00:00.000Z"

        exception_type: str | None = None
        if record.exc_info and record.exc_info[0] is not None:
            exception_type = getattr(record.exc_info[0], "__name__", "Exception")

        payload = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "exception_type": exception_type,
        }
        # All values are strings or null; ensure_ascii also encodes line breaks
        # and control characters so a record cannot forge additional log lines.
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def configure_logging(
    level: int = logging.INFO,
    *,
    logger: logging.Logger | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure a logger to write allowlisted JSON records to stdout.

    Repeated calls update the existing handler instead of adding duplicates.
    ``stream`` is primarily useful for embedding and tests; it defaults to
    ``sys.stdout``.
    """

    target = logging.getLogger() if logger is None else logger
    target.setLevel(level)

    handler = next(
        (item for item in target.handlers if getattr(item, _HANDLER_MARKER, False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        setattr(handler, _HANDLER_MARKER, True)
        target.addHandler(handler)
    elif stream is not None and isinstance(handler, logging.StreamHandler):
        handler.setStream(stream)

    handler.setLevel(level)
    handler.setFormatter(JSONFormatter())
    return target


__all__ = ["JSONFormatter", "configure_logging"]
