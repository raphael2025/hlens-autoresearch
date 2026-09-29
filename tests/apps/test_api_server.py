"""The local ASGI runtime of the read-only API: Uvicorn on 127.0.0.1 only (ADR-0063, B65).

Two halves:

- without Uvicorn (always run): the bind policy (``127.0.0.1`` only, one worker, no reload), the
  command line (no host / worker / reload option), the ``create_app`` wiring answered in-process,
  the optional-extra declaration (default dependencies unchanged; ``uvicorn==0.53.0`` locked under
  the ``api-server`` extra only), that ``apps.api`` never imports Uvicorn, and the clear exit 2
  without it;
- with Uvicorn (the optional ``api-server`` extra): a real ``python -m apps.api.serve`` subprocess
  on an ephemeral 127.0.0.1 port -- health, the configured read-only endpoints over real HTTP --
  then a graceful stop on SIGTERM / SIGINT (exit 0). Skipped, with the reason, when the extra is not
  installed in this environment.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import serve
from apps.api.serve import LOOPBACK, NonLoopbackBind, UvicornMissing, build_app, server_options

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "apps" / "web" / "fixtures"
KNOWLEDGE = REPO / "docs" / "research" / "knowledge"
START_TIMEOUT = 20.0
RUNNING = re.compile(r"Uvicorn running on http://(?P<host>[^:\s]+):(?P<port>\d+)")


# ---- the bind policy and the command line (no Uvicorn needed) ---------------------------------


def test_the_server_binds_127_0_0_1_only_with_one_worker_and_no_reload() -> None:
    options = server_options(8000)
    assert options["host"] == LOOPBACK == "127.0.0.1"
    assert (options["workers"], options["reload"], options["proxy_headers"]) == (1, False, False)
    assert server_options(0)["port"] == 0  # ephemeral


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "", "localhost", "::1", "::", "127.0.0.2", "192.168.1.10", " 127.0.0.1", "127.1"],
)
def test_any_other_bind_address_is_refused(host: str) -> None:
    with pytest.raises(NonLoopbackBind, match="127.0.0.1 only"):
        server_options(8000, host)
    with pytest.raises(NonLoopbackBind):
        serve.run(object(), port=8000, host=host)  # refused before Uvicorn is even imported


@pytest.mark.parametrize("port", [-1, 65536, True, "8000"])
def test_an_invalid_port_is_refused(port: object) -> None:
    with pytest.raises(ValueError, match="port"):
        server_options(port)  # type: ignore[arg-type]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_startup_signal_is_routed_to_uvicorn_and_previous_handler_is_restored(
    signum: signal.Signals,
) -> None:
    received: list[int] = []

    def server_handler(signal_number: int, _frame: object) -> None:
        received.append(signal_number)

    previous = signal.getsignal(signum)
    with serve._successful_signal_replay(server_handler):
        signal.raise_signal(signum)

    assert received == [signum]
    assert signal.getsignal(signum) == previous


def test_the_command_line_has_no_host_worker_or_reload_option() -> None:
    options = {o for action in serve.parser()._actions for o in action.option_strings}
    assert options == {
        "-h",
        "--help",
        "--port",
        "--reports-root",
        "--jobs-results",
        "--jobs-idempotent",
        "--knowledge",
    }
    for extra in (["--host", "0.0.0.0"], ["--workers", "4"], ["--reload"]):
        with pytest.raises(SystemExit) as caught:
            serve.parser().parse_args(extra)
        assert caught.value.code == 2


def test_an_out_of_range_port_on_the_command_line_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert serve.main(["--port", "70000"]) == 2
    assert "port must be" in capsys.readouterr().err


# ---- the create_app wiring (in-process; no Uvicorn needed) ------------------------------------


def _reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("*.md"))
    return root


def test_the_options_wire_the_existing_create_app_settings(tmp_path: Path) -> None:
    args = serve.parser().parse_args(
        ["--reports-root", str(_reports(tmp_path)), "--knowledge", str(KNOWLEDGE)]
    )
    client = TestClient(build_app(args))
    assert client.get("/health").json()["status"] == "ok"
    listing = client.get("/reports/validation_report")
    assert listing.status_code == 200 and listing.json()["reports"]
    search = client.post("/knowledge/search", json={"terms": ["momentum"]})
    assert search.status_code == 200 and search.json()["items"]
    assert client.get("/jobs").status_code == 503  # no journal given: as create_app answers


def test_nothing_given_is_answered_as_create_app_does_without_it() -> None:
    client = TestClient(build_app(serve.parser().parse_args([])))
    assert client.get("/health").status_code == 200
    assert client.post("/knowledge/search", json={"terms": ["momentum"]}).status_code == 503
    assert client.get("/jobs").status_code == 503


# ---- the optional extra (no Uvicorn needed) ----------------------------------------------------


def test_uvicorn_is_only_the_optional_api_server_extra() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert not any(dep.startswith("uvicorn") for dep in project["dependencies"])
    assert project["optional-dependencies"] == {"api-server": ["uvicorn==0.53.0"]}
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    [uvicorn] = [p for p in lock["package"] if p["name"] == "uvicorn"]
    assert uvicorn["version"] == "0.53.0"
    [this] = [p for p in lock["package"] if p["name"] == "hlens-autoresearch"]
    assert "uvicorn" not in {d["name"] for d in this.get("dependencies", [])}
    assert this["optional-dependencies"] == {"api-server": [{"name": "uvicorn"}]}


def test_apps_api_never_imports_uvicorn() -> None:
    code = "import sys, apps.api, apps.api.serve; print('uvicorn' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


def test_without_the_extra_main_exits_2_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # import uvicorn -> ModuleNotFoundError
    with pytest.raises(UvicornMissing, match="api-server"):
        serve.run(object(), port=0)
    assert serve.main(["--port", "0"]) == 2
    assert "optional api-server extra" in capsys.readouterr().err


# ---- a real Uvicorn subprocess on 127.0.0.1 (needs the api-server extra) -----------------------


class _Server:
    def __init__(self, process: subprocess.Popen[str], base_url: str, log: list[str]) -> None:
        self.process, self.base_url, self.log = process, base_url, log

    def stop(self, sig: signal.Signals) -> tuple[int, str]:
        self.process.send_signal(sig)
        try:
            self.process.wait(timeout=START_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            if self.process.stdout is not None:
                self.process.stdout.read()
            raise
        # _start() already consumed startup lines from this pipe. After a direct readline(),
        # Popen.communicate() may return ``None`` for stdout instead of the shutdown tail.
        # Drain the stream itself after process exit so the graceful-shutdown assertions see it.
        rest = self.process.stdout.read() if self.process.stdout is not None else ""
        return self.process.returncode, "".join(self.log) + rest


def _start(tmp_path: Path) -> _Server:
    argv = [
        sys.executable,
        "-m",
        "apps.api.serve",
        "--port",
        "0",
        "--reports-root",
        str(_reports(tmp_path)),
        "--knowledge",
        str(KNOWLEDGE),
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        argv, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    assert process.stdout is not None
    log: list[str] = []
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        if not ready:
            if process.poll() is not None:
                break
            continue
        line = process.stdout.readline()
        if not line:
            break
        log.append(line)
        match = RUNNING.search(line)
        if match:
            assert match["host"] == LOOPBACK, line
            return _Server(process, f"http://{LOOPBACK}:{match['port']}", log)
    process.kill()
    process.communicate()
    pytest.fail("the Uvicorn server did not start:\n" + "".join(log))


@pytest.fixture
def uvicorn_installed() -> Iterator[None]:
    pytest.importorskip(
        "uvicorn",
        reason="the optional api-server extra (uvicorn) is not installed in this environment "
        "(ADR-0063: installing it awaits explicit approval)",
    )
    yield


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_real_uvicorn_serves_on_loopback_and_stops_gracefully(
    uvicorn_installed: None, tmp_path: Path, sig: signal.Signals
) -> None:
    server = _start(tmp_path)
    try:
        with httpx.Client(base_url=server.base_url, timeout=10) as client:
            health = client.get("/health")
            assert health.status_code == 200 and health.json()["status"] == "ok"
            listing = client.get("/reports/validation_report")
            assert listing.status_code == 200 and listing.json()["reports"]
            report = listing.json()["reports"][0]
            detail = client.get(f"/reports/validation_report/{report['id']}")
            assert detail.status_code == 200 and detail.json() == report
            search = client.post("/knowledge/search", json={"terms": ["momentum"]})
            assert search.status_code == 200 and search.json()["items"]
            assert client.get("/jobs").status_code == 503  # not configured
            assert "server" not in health.headers  # server_header off
    finally:
        alive = server.process.poll() is None
        code, log = server.stop(sig) if alive else (server.process.returncode, "".join(server.log))
    assert alive, log  # it was still serving when the signal was sent
    assert code == 0, log
    assert "Shutting down" in log and "Finished server process" in log, log
    assert "0.0.0.0" not in log
