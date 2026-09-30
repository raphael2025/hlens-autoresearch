"""Production process host for an explicitly composed worker runtime.

The deployment supplies trusted Python code with ``--factory package.module:callable``. The
factory must return a context manager that yields a fully configured :class:`JobRunner`; it owns
the bus and journal lifecycle. The host never chooses handlers from message data and never imports
the research plane (ADR-0049 / ADR-0095).

Example::

    python -m apps.worker.serve --factory deployment.worker:runtime
"""

from __future__ import annotations

import argparse
import importlib
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast

from apps.worker.jobs import JobRunner

RuntimeFactory = Callable[[], AbstractContextManager[JobRunner]]


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="python -m apps.worker.serve",
        description="Run one worker consumer using an explicit trusted runtime factory.",
    )
    cli.add_argument(
        "--factory",
        required=True,
        metavar="MODULE:CALLABLE",
        help="trusted factory returning a context manager that yields a configured JobRunner",
    )
    return cli


def load_factory(spec: str) -> RuntimeFactory:
    """Load a deployment-supplied callable; message contents never participate in this import."""
    module_name, separator, attribute = spec.partition(":")
    if (
        not separator
        or not module_name
        or not attribute
        or ":" in attribute
        or any(not part.isidentifier() for part in module_name.split("."))
        or any(not part.isidentifier() for part in attribute.split("."))
    ):
        raise ValueError("factory must use MODULE:CALLABLE syntax")
    value: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise TypeError("factory target is not callable")
    return cast(RuntimeFactory, value)


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> object: ...


class _StopFlag:
    """Signal-safe stop request.

    A signal handler runs on the main thread, possibly while that thread is inside
    ``threading.Event.wait`` holding the event's non-reentrant internal lock; calling
    ``Event.set`` from the handler could then deadlock. Setting a plain attribute is atomic and
    lock-free, and :meth:`wait` sleeps in short slices so a request is observed promptly.
    """

    _SLICE = 0.05

    def __init__(self) -> None:
        self._requested = False

    def set(self) -> None:
        self._requested = True

    def is_set(self) -> bool:
        return self._requested

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._SLICE, remaining))
        return self._requested


def run(runner: JobRunner, stop: StopSignal, *, idle_wait: float = 1.0) -> None:
    """Poll one job at a time until stopped; finish an active job's durable ack boundary.

    The host idles only when a poll delivered no message at all; a redelivered message that was
    merely acknowledged (its result already journaled) is followed immediately by the next poll.
    """
    if idle_wait <= 0:
        raise ValueError("idle_wait must be positive")
    while not stop.is_set():
        runner.run_pending(limit=1)
        if runner.last_poll_count == 0:
            stop.wait(idle_wait)


class _SignalHandlers:
    """Install main-thread shutdown handlers that only set a lock-free flag."""

    def __init__(self, stop: _StopFlag) -> None:
        self.stop = stop
        self.previous: dict[signal.Signals, Any] = {}

    def __enter__(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.previous[signum] = signal.signal(signum, self._request_stop)

    def __exit__(self, *_: object) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def _request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop.set()


def serve(factory: RuntimeFactory, *, idle_wait: float = 1.0) -> None:
    """Compose and run the worker, releasing all factory-owned resources on every exit path."""
    stop = _StopFlag()
    with _SignalHandlers(stop):
        with factory() as runner:
            if not isinstance(runner, JobRunner):
                raise TypeError("runtime factory context must yield a JobRunner")
            run(runner, stop, idle_wait=idle_wait)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        factory = load_factory(args.factory)
        serve(factory)
    except Exception as error:
        # Avoid exposing job parameters or exception text in process logs.
        print(f"apps.worker.serve: stopped ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
