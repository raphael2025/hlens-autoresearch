"""A minimal stdlib HTTP/1.1 server driving the real ``apps.api`` ASGI app — TEST HARNESS ONLY.

``tests/apps/test_live_backend_smoke.py`` runs it as a separate process so every request crosses a
real socket and a process boundary::

    python -m tests.apps.live_server --port 0 --reports-root DIR --jobs-results FILE \\
        --knowledge DIR [--jobs-idempotent NAME ...]

It binds ``127.0.0.1`` only, prints ``{"port": <bound port>}`` on stdout once listening, and exits
0 on SIGTERM / SIGINT. uvicorn is not a project dependency, so this is an ``asyncio`` TCP server
that parses simple HTTP/1.1 requests (method, path, query, headers, ``Content-Length`` body) and
drives ``create_app(...)`` through the ASGI ``http`` protocol (``http.request`` /
``http.response.start`` / ``http.response.body``), answering one request per connection with
``Connection: close``.

This is **not a production server**: no keep-alive, chunked request bodies, TLS, timeouts,
lifespan events or back-pressure. A production deployment of ``apps/api`` would use an ASGI server
chosen by a later decision (none is chosen or depended on today).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections.abc import Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote

__all__ = ["main", "serve"]

HOST = "127.0.0.1"


async def serve(app: Any, port: int = 0) -> None:
    """Serve ``app`` (ASGI 3) on ``127.0.0.1:port`` (0 = ephemeral) until SIGTERM / SIGINT."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _exchange(app, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the client went away
        finally:
            writer.close()

    server = await asyncio.start_server(handle, HOST, port)
    bound = server.sockets[0].getsockname()[1]
    print(json.dumps({"port": bound}), flush=True)
    async with server:  # leaving closes the listener and waits for open connections
        await stop.wait()


async def _exchange(app: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    request_line, *header_lines = head.split("\r\n")
    method, target, _version = request_line.split(" ", 2)
    headers: list[tuple[bytes, bytes]] = []
    length = 0
    for line in header_lines:
        if not line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip()
        headers.append((name.encode("latin-1"), value.encode("latin-1")))
        if name == "content-length":
            length = int(value)
    body = await reader.readexactly(length) if length else b""
    raw_path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": headers,
        "client": writer.get_extra_info("peername")[:2],
        "server": writer.get_extra_info("sockname")[:2],
    }
    done = asyncio.Event()
    request_sent = False
    status = 500
    response_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()  # the whole response is out: the client is gone for the app
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, response_headers
        if message["type"] == "http.response.start":
            status = int(message["status"])
            response_headers = [(bytes(k).lower(), bytes(v)) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))
            if not message.get("more_body", False):
                done.set()

    await app(scope, receive, send)
    done.set()
    payload = b"".join(chunks)
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}".encode("latin-1")]
    lines += [k + b": " + v for k, v in response_headers if k != b"connection"]
    if not any(k == b"content-length" for k, _ in response_headers):
        lines.append(b"content-length: " + str(len(payload)).encode("ascii"))
    lines.append(b"connection: close")
    writer.write(b"\r\n".join(lines) + b"\r\n\r\n" + payload)
    await writer.drain()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--reports-root", type=Path)
    parser.add_argument("--jobs-results", type=Path)
    parser.add_argument("--knowledge", type=Path, help="LocalKnowledgeProvider items directory")
    parser.add_argument("--jobs-idempotent", nargs="*", default=[])
    args = parser.parse_args(argv)

    from apps.api import create_app
    from plugins.knowledge import LocalKnowledgeProvider

    app = create_app(
        knowledge=None if args.knowledge is None else LocalKnowledgeProvider(args.knowledge),
        reports_root=args.reports_root,
        jobs_results=args.jobs_results,
        jobs_idempotent=tuple(args.jobs_idempotent),
    )
    asyncio.run(serve(app, args.port))


if __name__ == "__main__":
    main()
