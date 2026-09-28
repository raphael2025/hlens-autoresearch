import json
import logging
import sys
from io import StringIO

from infrastructure.observability.logging import JSONFormatter, configure_logging


def test_json_formatter_emits_fixed_fields_and_utc_timestamp() -> None:
    record = logging.LogRecord(
        name="service.worker",
        level=logging.WARNING,
        pathname=__file__,
        lineno=12,
        msg="job %s failed",
        args=("abc",),
        exc_info=None,
    )
    record.created = 1_700_000_000.1234
    record.secret = "must not be serialized"

    encoded = JSONFormatter().format(record)
    payload = json.loads(encoded)

    assert list(payload) == ["timestamp", "level", "logger", "message", "exception_type"]
    assert payload == {
        "timestamp": "2023-11-14T22:13:20.123Z",
        "level": "WARNING",
        "logger": "service.worker",
        "message": "job abc failed",
        "exception_type": None,
    }
    assert "must not be serialized" not in encoded


def test_json_formatter_escapes_newlines_and_omits_exception_details() -> None:
    try:
        raise RuntimeError("password=hunter2\nforged log line")
    except RuntimeError:
        record = logging.LogRecord(
            name="service",
            level=logging.ERROR,
            pathname=__file__,
            lineno=30,
            msg="bad input\nforged log line",
            args=(),
            exc_info=sys.exc_info(),
        )

    encoded = JSONFormatter().format(record)
    payload = json.loads(encoded)

    assert "\n" not in encoded
    assert payload["message"] == "bad input\nforged log line"
    assert payload["exception_type"] == "RuntimeError"
    assert "hunter2" not in encoded


def test_json_formatter_handles_broken_message_formatting() -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("sensitive formatting failure")

    record = logging.LogRecord(
        name="service",
        level=logging.INFO,
        pathname=__file__,
        lineno=48,
        msg="%s",
        args=(Broken(),),
        exc_info=None,
    )

    assert json.loads(JSONFormatter().format(record))["message"] == "<unformattable log message>"


def test_configure_logging_writes_json_and_is_idempotent() -> None:
    output = StringIO()
    target = logging.Logger("isolated.ops2.test")

    configured = configure_logging(logging.DEBUG, logger=target, stream=output)
    configure_logging(logging.DEBUG, logger=target, stream=output)
    configured.debug("ready")

    assert configured is target
    assert len(target.handlers) == 1
    assert json.loads(output.getvalue()) == {
        "timestamp": json.loads(output.getvalue())["timestamp"],
        "level": "DEBUG",
        "logger": "isolated.ops2.test",
        "message": "ready",
        "exception_type": None,
    }
