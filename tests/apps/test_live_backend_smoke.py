"""Live-backend smoke: the real ``apps.api`` in a separate process, over real HTTP on 127.0.0.1
(ADR-0048 read-only console; 2026-09-26, CODE_COMPLETE / DEBUG_PENDING).

Every other API test drives the app in-process (``TestClient``) and the console's tests read the
fixture files directly. This module is the full-stack evidence without a browser:

- a temporary report root holding every ``ReportKind`` (a copy of ``apps/web/fixtures/``, whose
  files are the real ``research/reports`` writers' output — pinned by
  ``tests/research/reports/test_console_fixture_writers.py`` / ``test_console_fixtures.py``) plus
  one malformed file, a results journal written by a real ``JobRunner`` (succeeded, failed and
  interrupted jobs) and a knowledge directory (a copy of ``docs/research/knowledge/``);
- ``create_app(...)`` served by a **subprocess** (``python -m tests.apps.live_server --port 0
  ...``) on ``127.0.0.1`` and an ephemeral port. uvicorn is not a project dependency, so the child
  is ``tests/apps/live_server.py``: a minimal stdlib ASGI/HTTP-1.1 test server (not a production
  server) — no new dependency;
- ``httpx`` exercises every operation of the committed ``apps/api/openapi.json`` (and the served
  ``/openapi.json`` must equal it); every answer's status must be declared for its operation and
  its body must conform to the committed schema (a small JSON-Schema subset checker over the
  keywords the document uses — ``jsonschema`` is not installed — plus a pydantic round trip
  through the response model the app declares, which also catches undeclared extra fields);
- a second server without knowledge provider / jobs journal answers the 503 paths;
- a third ("broken") server answers the remaining error paths over real HTTP: 502 from a knowledge
  provider raising ``KnowledgeProviderError`` (injected by the test server's test-only
  ``--knowledge-error`` flag; no hook in ``apps/``), 500 from ``/jobs`` / ``/jobs/{id}`` on a copy
  of the real journal whose hash chain is broken, and the catch-all 500 from a report read that
  raises an exception nothing maps (the test server's test-only ``--fault-report-read``); each body
  is the declared ``ApiError`` and carries no server path, traceback or exception type;
- ``apps/web/scripts/live-smoke.mjs`` then runs the console's own client (``src/api.ts``),
  view-model helpers (``src/lib``) and a server-side render of every page against the full
  server, and the error paths of the unconfigured (503) and broken (502 / 500) servers through the
  client and the pages that show them (skipped with a reason when ``node`` / the installed
  ``apps/web/node_modules`` is absent).

Not proven here: pixel rendering, browser effects, user interaction — manual browser acceptance
stays open (apps/web/README.md, apps/api/README.md "Live-backend smoke").
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, TypeAdapter

from apps.api.app import (
    INTERNAL_ERROR,
    JOBS_NOT_CONFIGURED,
    KNOWLEDGE_NOT_CONFIGURED,
    ApiError,
    ContractNames,
    Health,
    JobList,
    JobView,
    LifecycleTransition,
)
from apps.api.store import ReportEnvelope, ReportKind, ReportListing
from core.contracts.knowledge import KnowledgeResult

REPO = Path(__file__).resolve().parents[2]
OPENAPI = json.loads((REPO / "apps" / "api" / "openapi.json").read_text(encoding="utf-8"))
FIXTURES = REPO / "apps" / "web" / "fixtures"
KNOWLEDGE = REPO / "docs" / "research" / "knowledge"
WEB = REPO / "apps" / "web"
LIVE_SMOKE_JS = WEB / "scripts" / "live-smoke.mjs"

#: A report file with a safe, well-formed id whose content is not JSON: listed as ``invalid``,
#: its detail is the store's 422.
MALFORMED_KIND = ReportKind.VALIDATION_REPORT
MALFORMED_ID = "f" * 64

#: The broken server's unmapped-exception trigger: the test server's test-only
#: ``--fault-report-read`` makes every ``ReportStore`` read raise ``RuntimeError(FAULT)`` in that
#: process (no hook in ``apps/``), so the listing and the detail of a kind with one report file
#: reach the app's catch-all 500. ``FAULT`` names a server path (``{root}`` = the temporary work
#: directory) that must not reach the client. (The earlier real-data trigger -- an integer literal
#: over the int-conversion limit -- was a store bug, now fixed: malformed entry, see test_reports.)
ODD_KIND = ReportKind.STATE_STRATEGY_MATRIX
ODD_ID = "e" * 64
FAULT = "injected report read fault at {root}/secret.json"

#: The broken server's journal: a copy of the real one whose line 2 no longer links to line 1.
TAMPERED_DETAIL = "job results journal failed verification: results.jsonl:2 breaks the hash chain"
#: What the broken server's provider raises (a realistic OSError text naming a server path; the
#: ``{root}`` is the temporary work directory) and the 502 detail it must become (path-free).
KNOWLEDGE_FAILURE = "items.json: unreadable: [Errno 13] Permission denied: '{root}/items.json'"
KNOWLEDGE_FAILURE_DETAIL = (
    "knowledge provider could not answer: items.json: unreadable: [Errno 13] Permission denied: "
    "'items.json'"
)

#: The response models ``apps.api`` declares, by their committed OpenAPI component name.
#: (``HTTPValidationError`` is FastAPI's own schema, checked against the document only.)
MODELS: Mapping[str, type[BaseModel]] = {
    "ApiError": ApiError,
    "ContractNames": ContractNames,
    "Health": Health,
    "JobList": JobList,
    "JobView": JobView,
    "KnowledgeResult": KnowledgeResult,
    "LifecycleTransition": LifecycleTransition,
    "ReportEnvelope": ReportEnvelope,
    "ReportListing": ReportListing,
}

START_TIMEOUT = 20.0


# --- the parent: data, server lifecycle ---------------------------------------------------------


class Crash(BaseException):
    """A process death inside a handler (not an ``Exception``: no retry sees it)."""


def _boom(_: Mapping[str, Any]) -> None:
    raise ValueError("bad input")


def _die(_: Mapping[str, Any]) -> None:
    raise Crash


def _write_jobs(path: Path) -> dict[str, str]:
    """A real ``JobRunner`` results journal: succeeded, failed (2 attempts), interrupted."""
    from apps.worker import JobRunner, JobSpec
    from infrastructure.event_bus import InMemoryEventBus

    runner = JobRunner(
        InMemoryEventBus(),
        consumer="w",
        topic="jobs",
        handlers={"double": lambda p: {"doubled": int(p["x"]) * 2}, "boom": _boom, "die": _die},
        results=path,
        max_attempts=2,
    )
    ids = {
        "succeeded": runner.submit(JobSpec("double", {"x": 21})),
        "failed": runner.submit(JobSpec("boom", {})),
        "interrupted": runner.submit(JobSpec("die", {"id": "a"})),
    }
    with pytest.raises(Crash):
        runner.run_pending()
    return ids


def _break_chain(source: Path, target: Path) -> None:
    """``target`` = ``source`` (a real results journal) with line 2's ``prev_hash`` replaced by a
    different hash: the history splices in a line that does not follow line 1."""
    lines = source.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2, "the real journal is too short to tamper with"
    entry = json.loads(lines[1])
    assert entry["prev_hash"] != "1" * 64
    entry["prev_hash"] = "1" * 64
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class Server:
    base_url: str
    process: subprocess.Popen[bytes]
    stderr: Path


def _start(options: Sequence[str], workdir: Path, name: str) -> Server:
    stderr = workdir / f"{name}.stderr"
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with stderr.open("wb") as err:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.apps.live_server", "--port", "0", *options],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=err,
        )
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], START_TIMEOUT)
    line = process.stdout.readline() if ready else b""
    if not line:
        process.kill()
        process.wait()
        raise AssertionError(f"{name} server did not start: {stderr.read_text(errors='replace')}")
    port = int(json.loads(line)["port"])
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return Server(base_url, process, stderr)
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline or process.poll() is not None:
            process.kill()
            process.wait()
            raise AssertionError(f"{name} server never healthy: {stderr.read_text()}")
        time.sleep(0.05)


def _stop(server: Server) -> int | str:
    """SIGTERM, then the exit code (``"killed"`` when it did not exit within 10 s)."""
    server.process.send_signal(signal.SIGTERM)
    try:
        return server.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.process.kill()
        server.process.wait()
        return "killed"
    finally:
        assert server.process.stdout is not None
        server.process.stdout.close()


@dataclass
class Live:
    full: Server
    bare: Server
    broken: Server
    work: Path
    reports_root: Path
    jobs: dict[str, str]


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    work = tmp_path_factory.mktemp("live_backend")
    reports_root = work / "reports"
    shutil.copytree(FIXTURES, reports_root, ignore=shutil.ignore_patterns("*.md"))
    (reports_root / MALFORMED_KIND.value / f"{MALFORMED_ID}.json").write_text(
        "{not json", encoding="utf-8"
    )
    knowledge = work / "knowledge"
    shutil.copytree(KNOWLEDGE, knowledge)
    results = work / "jobs" / "results.jsonl"
    results.parent.mkdir()
    jobs = _write_jobs(results)
    broken_reports = work / "reports_broken"
    (broken_reports / ODD_KIND.value).mkdir(parents=True)
    (broken_reports / ODD_KIND.value / f"{ODD_ID}.json").write_text("{}", encoding="utf-8")
    tampered = work / "jobs_tampered" / "results.jsonl"
    tampered.parent.mkdir()
    _break_chain(results, tampered)
    options = {
        "full": [
            "--reports-root",
            str(reports_root),
            "--jobs-results",
            str(results),
            "--knowledge",
            str(knowledge),
        ],
        "bare": [],
        "broken": [
            "--reports-root",
            str(broken_reports),
            "--jobs-results",
            str(tampered),
            "--knowledge-error",
            KNOWLEDGE_FAILURE.format(root=knowledge),
            "--fault-report-read",
            FAULT.format(root=work),
        ],
    }
    servers: dict[str, Server] = {}
    try:
        for name, argv in options.items():
            servers[name] = _start(argv, work, name)
    except BaseException:
        for server in servers.values():
            _stop(server)
        raise
    try:
        yield Live(
            servers["full"],
            servers["bare"],
            servers["broken"],
            work=work,
            reports_root=reports_root,
            jobs=jobs,
        )
    finally:
        codes = {name: _stop(server) for name, server in servers.items()}
    assert codes == dict.fromkeys(options, 0), (
        codes,
        {name: server.stderr.read_text() for name, server in servers.items()},
    )


# --- schema conformance against the committed openapi.json --------------------------------------

_ANNOTATIONS = frozenset({"title", "description", "default", "format", "examples"})
_TYPES: Mapping[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema["$ref"]
    assert ref.startswith("#/components/schemas/"), ref
    resolved: Mapping[str, Any] = OPENAPI["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    return resolved


def _violations(value: Any, schema: Mapping[str, Any], at: str = "$") -> list[str]:
    """Where ``value`` breaks ``schema`` (the JSON-Schema keywords ``openapi.json`` uses; any other
    keyword is itself reported, so nothing is silently skipped)."""
    problems: list[str] = []
    for keyword, rule in schema.items():
        if keyword in _ANNOTATIONS:
            continue
        if keyword == "$ref":
            problems += _violations(value, _resolve(schema), at)
        elif keyword == "type":
            names = rule if isinstance(rule, list) else [rule]
            if not any(_TYPES[name](value) for name in names):
                problems.append(f"{at}: {value!r} is not of type {rule}")
        elif keyword == "anyOf":
            if all(_violations(value, option, at) for option in rule):
                problems.append(f"{at}: matches no anyOf option")
        elif keyword == "enum":
            if value not in rule:
                problems.append(f"{at}: {value!r} not in {rule}")
        elif keyword == "const":
            if value != rule:
                problems.append(f"{at}: {value!r} != const {rule!r}")
        elif keyword in ("properties", "required", "additionalProperties"):
            if not isinstance(value, dict):
                continue
            if keyword == "required":
                problems += [f"{at}: missing {key!r}" for key in rule if key not in value]
            elif keyword == "properties":
                for key, sub in rule.items():
                    if key in value:
                        problems += _violations(value[key], sub, f"{at}.{key}")
            else:
                declared = schema.get("properties", {})
                for key, item in value.items():
                    if key in declared:
                        continue
                    if rule is False:
                        problems.append(f"{at}: undeclared key {key!r}")
                    elif isinstance(rule, dict):
                        problems += _violations(item, rule, f"{at}.{key}")
        elif keyword == "items":
            if isinstance(value, list):
                for index, item in enumerate(value):
                    problems += _violations(item, rule, f"{at}[{index}]")
        elif keyword in ("minimum", "maximum"):
            if _TYPES["number"](value) and (value < rule if keyword == "minimum" else value > rule):
                problems.append(f"{at}: {value} violates {keyword} {rule}")
        elif keyword == "minLength":
            if isinstance(value, str) and len(value) < rule:
                problems.append(f"{at}: shorter than {rule}")
        elif keyword == "pattern":
            if isinstance(value, str) and re.search(rule, value) is None:
                problems.append(f"{at}: {value!r} does not match {rule!r}")
        else:
            problems.append(f"{at}: unsupported schema keyword {keyword!r}")
    return problems


def _model_of(schema: Mapping[str, Any]) -> Any:
    """The pydantic type the app declares for a (non-``anyOf``) response schema, by component
    name; ``None`` for FastAPI's own ``HTTPValidationError``."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name == "HTTPValidationError":
            return None
        return MODELS[name]
    if schema.get("type") == "array" and "$ref" in schema.get("items", {}):
        return list[_model_of(schema["items"])]  # type: ignore[misc]
    raise AssertionError(f"unmapped response schema {schema}")


def _operation(method: str, path: str) -> tuple[str, Mapping[str, Any]]:
    for template, operations in OPENAPI["paths"].items():
        pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path) and method.lower() in operations:
            return template, operations[method.lower()]
    raise AssertionError(f"{method} {path} is not an operation of the committed openapi.json")


def _round_trips(model: Any, raw: bytes, data: Any) -> bool:
    adapter: TypeAdapter[Any] = TypeAdapter(model)
    try:
        parsed = adapter.validate_json(raw)
    except ValueError:
        return False
    return bool(adapter.dump_python(parsed, mode="json", by_alias=True) == data)


@dataclass
class Checked:
    """An HTTP client that checks every answer against the committed document and records
    ``(method, template, status)``."""

    base_url: str
    seen: set[tuple[str, str, int]] = field(default_factory=set)

    def call(self, method: str, path: str, status: int, body: Any = None) -> Any:
        response = httpx.request(method, self.base_url + path, json=body, timeout=10.0)
        assert response.status_code == status, (method, path, response.status_code, response.text)
        template, operation = _operation(method, path)
        declared = operation["responses"]
        assert str(status) in declared, f"{method} {template}: {status} is not declared"
        assert response.headers["content-type"] == "application/json"
        schema = declared[str(status)]["content"]["application/json"]["schema"]
        data = response.json()
        assert _violations(data, schema) == [], (method, path, _violations(data, schema))
        # the declared response model of the option the body conforms to: it validates and
        # re-serializes to exactly the served JSON (so no undeclared extra field is served)
        options = schema["anyOf"] if "anyOf" in schema else [schema]
        models = [_model_of(option) for option in options if not _violations(data, option)]
        if None not in models:
            assert any(_round_trips(model, response.content, data) for model in models), (
                method,
                path,
                "no declared response model round-trips",
                data,
            )
        self.seen.add((method.upper(), template, status))
        return data


def _fixture_payloads(kind: ReportKind) -> dict[str, Any]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURES / kind.value).glob("*.json"))
    }


# --- tests --------------------------------------------------------------------------------------


def test_every_openapi_operation_answers_its_declared_schema_over_real_http(live: Live) -> None:
    full = Checked(live.full.base_url)

    served = httpx.get(f"{live.full.base_url}/openapi.json", timeout=10.0)
    assert served.status_code == 200 and served.json() == OPENAPI  # committed == live

    assert full.call("GET", "/health", 200)["status"] == "ok"
    assert full.call("GET", "/healthz", 200)["status"] == "ok"
    # ready: every configured read source (knowledge provider, reports root) answers
    assert full.call("GET", "/readyz", 200)["status"] == "ready"
    assert full.call("GET", "/contracts", 200)
    assert full.call("GET", "/lifecycle/transitions", 200)

    # every report kind: list + every detail, the payload exactly the real writers' file
    for kind in ReportKind:
        listing = full.call("GET", f"/reports/{kind.value}", 200)
        expected = _fixture_payloads(kind)
        assert {item["id"] for item in listing["reports"]} == set(expected), kind
        for item in listing["reports"]:
            detail = full.call("GET", f"/reports/{kind.value}/{item['id']}", 200)
            assert detail == item and detail["payload"] == expected[item["id"]]
        malformed = [bad["id"] for bad in listing["invalid"]]
        assert malformed == ([MALFORMED_ID] if kind is MALFORMED_KIND else []), kind

    # report errors: malformed file (store's 422), unsafe id, missing id, unknown kind
    bad = f"/reports/{MALFORMED_KIND.value}/{MALFORMED_ID}"
    detail = full.call("GET", bad, 422)["detail"]
    assert detail.startswith(f"{MALFORMED_KIND.value}/{MALFORMED_ID} is malformed: ")
    assert str(live.reports_root) not in detail  # never a server path
    full.call("GET", f"/reports/{MALFORMED_KIND.value}/.hidden", 400)
    full.call("GET", f"/reports/{MALFORMED_KIND.value}/{'0' * 64}", 404)
    assert isinstance(full.call("GET", "/reports/no_such_kind", 422)["detail"], list)
    assert isinstance(full.call("GET", "/reports/no_such_kind/x", 422)["detail"], list)

    # jobs: the real runner's journal, three statuses; 400 / 404
    jobs = full.call("GET", "/jobs", 200)
    by_id = {job["job_id"]: job for job in jobs["jobs"]}
    assert set(by_id) == set(live.jobs.values())
    for status, job_id in live.jobs.items():
        job = full.call("GET", f"/jobs/{job_id}", 200)
        assert job == by_id[job_id] and job["status"] == status
    assert by_id[live.jobs["succeeded"]]["result"] == {"doubled": 42}
    assert by_id[live.jobs["failed"]]["error"] == "ValueError: bad input"
    full.call("GET", "/jobs/not-a-hash", 400)
    full.call("GET", f"/jobs/{'0' * 64}", 404)

    # knowledge search: hits through the provider; request validation
    result = full.call("POST", "/knowledge/search", 200, {"terms": ["momentum"], "limit": 10})
    assert result["items"] and result["provider"]
    full.call("POST", "/knowledge/search", 422, {"limit": 0})

    # the unconfigured server: explicit 503s with the stable details, empty report listings
    bare = Checked(live.bare.base_url, full.seen)
    search = bare.call("POST", "/knowledge/search", 503, {"terms": ["momentum"]})
    assert search == {"detail": KNOWLEDGE_NOT_CONFIGURED}
    assert bare.call("GET", "/jobs", 503) == {"detail": JOBS_NOT_CONFIGURED}
    assert bare.call("GET", f"/jobs/{'0' * 64}", 503) == {"detail": JOBS_NOT_CONFIGURED}
    for kind in ReportKind:
        empty = bare.call("GET", f"/reports/{kind.value}", 200)
        assert empty["reports"] == [] and empty["invalid"] == []

    # every operation of the committed document answered 200 at least once over real HTTP
    operations = {
        (method.upper(), template)
        for template, methods in OPENAPI["paths"].items()
        for method in methods
    }
    answered = {(method, template) for method, template, status in full.seen if status == 200}
    assert answered == operations


def _assert_api_error_without_leaks(
    data: Any, method: str, path: str, status: int, work: Path
) -> None:
    """``status`` is declared for the operation as exactly ``ApiError``; the body is one; it
    names no server path (the temporary work root holding every served file, the repository
    root) and no traceback / exception type."""
    template, operation = _operation(method, path)
    schema = operation["responses"][str(status)]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ApiError"}, (method, template, status)
    ApiError.model_validate(data)
    text = json.dumps(data)
    leaks = (str(work), str(REPO), "Traceback", 'File "', ".py", "Error:", "Exception")
    for leak in (*leaks, "KnowledgeProviderError", "JournalCorrupted", "ValueError"):
        assert leak not in text, (method, path, leak, text)


def test_the_error_paths_answer_declared_api_errors_over_real_http(live: Live) -> None:
    broken = Checked(live.broken.base_url)  # asserts: status declared, schema, model round trip

    # 502: the configured provider raises KnowledgeProviderError (a message naming a server path)
    search = ("POST", "/knowledge/search")
    data = broken.call(*search, 502, {"terms": ["momentum"]})
    assert data == {"detail": KNOWLEDGE_FAILURE_DETAIL}
    _assert_api_error_without_leaks(data, *search, 502, live.work)

    # 500: the job results journal is a tampered copy of the real runner's journal
    for path in ("/jobs", f"/jobs/{live.jobs['succeeded']}"):
        data = broken.call("GET", path, 500)
        assert data == {"detail": TAMPERED_DETAIL}
        _assert_api_error_without_leaks(data, "GET", path, 500, live.work)

    # catch-all 500: the (injected) report read raises an exception no route maps
    for path in (f"/reports/{ODD_KIND.value}", f"/reports/{ODD_KIND.value}/{ODD_ID}"):
        data = broken.call("GET", path, 500)
        assert data == {"detail": INTERNAL_ERROR}
        _assert_api_error_without_leaks(data, "GET", path, 500, live.work)
        assert "injected" not in json.dumps(data) and "secret" not in json.dumps(data)
    # it really was the unmapped exception: the server's log (never the client) has its traceback
    log = live.broken.stderr.read_text(errors="replace")
    fault = FAULT.format(root=live.work)
    assert log.count(f"RuntimeError: {fault}") == 2 and "Traceback" in log, log

    assert broken.seen == {
        ("POST", "/knowledge/search", 502),
        ("GET", "/jobs", 500),
        ("GET", "/jobs/{job_id}", 500),
        ("GET", "/reports/{kind}", 500),
        ("GET", "/reports/{kind}/{report_id}", 500),
    }


def test_the_console_client_and_view_models_run_against_the_live_api(live: Live) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the console half of the live smoke cannot run")
    if not (WEB / "node_modules" / "esbuild").is_dir():
        pytest.skip("apps/web/node_modules is not installed (npm ci): no esbuild to bundle")
    env = {
        **os.environ,
        "BASE_URL": live.full.base_url,
        "BARE_BASE_URL": live.bare.base_url,
        "EXPECT_INVALID": f"{MALFORMED_KIND.value}/{MALFORMED_ID}",
        "EXPECT_JOBS": json.dumps(live.jobs),
        "BROKEN_BASE_URL": live.broken.base_url,
        "EXPECT_BROKEN": json.dumps(
            {
                "knowledge": KNOWLEDGE_FAILURE_DETAIL,
                "jobs": TAMPERED_DETAIL,
                "internal": INTERNAL_ERROR,
                "job_id": live.jobs["succeeded"],
                "report": f"{ODD_KIND.value}/{ODD_ID}",
                # what the server knows and the client must never see
                "forbidden": [
                    str(live.work),
                    str(REPO),
                    "Traceback",
                    "injected",
                    "secret",
                    "KnowledgeProviderError",
                    "JournalCorrupted",
                    "RuntimeError",
                ],
            }
        ),
    }
    completed = subprocess.run(
        [node, str(LIVE_SMOKE_JS)],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    print(completed.stdout)  # the per-step "live-smoke: ok - ..." lines (pytest -s / -rP)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "live-smoke: OK" in completed.stdout, completed.stdout
    # the error paths really ran (the script skips each block without its server)
    for step in (
        "unconfigured server: Knowledge Search / Jobs pages show the 503 error state",
        "broken server: knowledge 502, tampered jobs journal 500, catch-all report 500",
        "broken server: KnowledgeSearch / Jobs / StateStrategyMatrices pages show the 502 / 500",
    ):
        assert f"live-smoke: ok - {step}" in completed.stdout, completed.stdout
