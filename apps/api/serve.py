"""Local runtime of the read-only research API: Uvicorn on ``127.0.0.1`` only (ADR-0063, B65).

::

    uv run --extra api-server python -m apps.api.serve --port 8000 \\
        [--reports-root DIR] [--jobs-results FILE [--jobs-idempotent NAME ...]] [--knowledge DIR]

The API has no authentication, so it is served on this machine only: the host is fixed to
``127.0.0.1`` (there is no ``--host`` option, and ``server_options`` / ``run`` refuse any other
address -- ``0.0.0.0``, ``localhost``, ``::1``, a LAN address), one worker, reload off, no proxy
headers trusted. Public deployment, TLS, authentication, a reverse proxy and HA are out of scope
(ADR-0063). ``--port 0`` binds an ephemeral port; Uvicorn logs the bound one.

The options wire the existing ``create_app`` settings: ``--reports-root`` (the report store),
``--jobs-results`` / ``--jobs-idempotent`` (the worker's results journal and its idempotent handler
names, which must equal the runner's), ``--knowledge`` (a ``LocalKnowledgeProvider`` items
directory). Anything not given is answered as ``create_app`` does without it (empty report
listings, 503 for jobs / knowledge).

Uvicorn is the optional ``api-server`` extra: ``apps.api`` itself never imports it, and this module
imports it only when it is about to serve. Without the extra, ``main`` exits 2 with a message
instead of a traceback. SIGINT / SIGTERM stop the server gracefully (Uvicorn's own handling); the
process then exits 0.
"""

from __future__ import annotations

import argparse
import importlib
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Final

__all__ = [
    "LOOPBACK",
    "NonLoopbackBind",
    "UvicornMissing",
    "build_app",
    "main",
    "parser",
    "run",
    "server_options",
]

#: The only address the unauthenticated API is ever bound to (ADR-0063).
LOOPBACK: Final = "127.0.0.1"


class NonLoopbackBind(ValueError):
    """A bind address other than ``127.0.0.1`` was asked for (ADR-0063 refuses it)."""


class UvicornMissing(RuntimeError):
    """The optional ``api-server`` extra (Uvicorn) is not installed."""


def server_options(port: int, host: str = LOOPBACK) -> dict[str, Any]:
    """The Uvicorn settings ADR-0063 allows: ``127.0.0.1``, one worker, no reload."""
    if host != LOOPBACK:
        raise NonLoopbackBind(
            f"refusing to bind {host!r}: the unauthenticated API is served on {LOOPBACK} only "
            "(ADR-0063)"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError(f"port must be an integer in 0..65535, got {port!r}")
    return {
        "host": LOOPBACK,
        "port": port,
        "workers": 1,
        "reload": False,
        "proxy_headers": False,
        "server_header": False,
        "log_level": "info",
    }


def _uvicorn() -> ModuleType:
    try:
        return importlib.import_module("uvicorn")
    except ModuleNotFoundError as error:
        raise UvicornMissing(
            "uvicorn is not installed: it is the optional api-server extra "
            "(uv run --extra api-server ...; ADR-0063)"
        ) from error


@contextmanager
def _successful_signal_replay(handler: Any) -> Iterator[None]:
    """Route startup signals to Uvicorn and keep its post-shutdown replay in-process.

    Uvicorn restores the handlers it replaced and re-sends captured SIGINT / SIGTERM after its
    cleanup finishes. Installing the server's handler before ``Server.run`` closes the brief
    startup window before Uvicorn captures signals. The restored handler receives Uvicorn's replay
    as a normal call, so shutdown still finishes with a successful process status.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous: dict[signal.Signals, Any] = {}

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, handler)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run(app: Any, *, port: int, host: str = LOOPBACK) -> None:
    """Serve ``app`` with Uvicorn until SIGINT / SIGTERM (blocking)."""
    options = server_options(port, host)  # refuse a non-loopback bind before importing anything
    from infrastructure.observability.logging import configure_logging

    uvicorn = _uvicorn()
    configure_logging()
    server = uvicorn.Server(
        uvicorn.Config(app, **options, access_log=False, log_config=None)
    )
    with _successful_signal_replay(server.handle_exit):
        server.run()


def parser() -> argparse.ArgumentParser:
    """The command line: the ``create_app`` settings and a port; no host / worker / reload."""
    cli = argparse.ArgumentParser(
        prog="python -m apps.api.serve",
        description=f"Serve the read-only research API on {LOOPBACK} with Uvicorn (ADR-0063).",
    )
    cli.add_argument("--port", type=int, default=8000, help="TCP port on 127.0.0.1 (0: ephemeral)")
    cli.add_argument("--reports-root", type=Path, help="report store root (research reports)")
    cli.add_argument("--jobs-results", type=Path, help="the worker's results journal")
    cli.add_argument(
        "--jobs-idempotent",
        nargs="*",
        default=[],
        help="handler names the worker declares idempotent (must equal the runner's)",
    )
    cli.add_argument("--knowledge", type=Path, help="LocalKnowledgeProvider items directory")
    return cli


def build_app(args: argparse.Namespace) -> Any:
    """``create_app`` with the settings in ``args`` (nothing is served here)."""
    from apps.api import create_app

    knowledge = None
    if args.knowledge is not None:
        from plugins.knowledge import LocalKnowledgeProvider

        knowledge = LocalKnowledgeProvider(args.knowledge)
    return create_app(
        knowledge=knowledge,
        reports_root=args.reports_root,
        jobs_results=args.jobs_results,
        jobs_idempotent=tuple(args.jobs_idempotent),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        options = server_options(args.port)
        _uvicorn()
    except (UvicornMissing, ValueError) as error:
        print(f"apps.api.serve: {error}", file=sys.stderr)
        return 2
    run(build_app(args), port=options["port"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
