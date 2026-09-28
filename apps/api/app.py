"""FastAPI application (ADR-0048; framework, FRAMEWORK_IMPLEMENTED / NOT_VALIDATED).

Read-only endpoints over the domain and plugins; no business rules live here, and nothing imports
``research/`` (01-system.md §3). Providers are injected, so tests and deployments choose them.

Error mapping (2026-09-26; CODE_COMPLETE / DEBUG_PENDING). Every error this module raises itself
has the body ``{"detail": "<message>"}`` (:class:`ApiError`, FastAPI's ``HTTPException`` shape);
only FastAPI's own request validation answers 422 with a list ``detail``:

| status | when |
|---|---|
| 400 | an unsafe report id / a job id that is not a 64-hex content hash |
| 404 | a report or job that does not exist |
| 422 | a report file that is malformed or fails its kind's contract / identity check; request |
|     | validation (FastAPI's list ``detail``) |
| 500 | the job results journal fails verification (tampered / broken; never partial data); |
|     | any unexpected server error (catch-all: the stable ``INTERNAL_ERROR`` detail only) |
| 502 | the configured knowledge provider could not answer honestly (``KnowledgeProviderError``) |
| 503 | the knowledge provider / job results journal is not configured |

No error body carries a server filesystem path (2026-09-26): a malformed report answers with the
store's path-free reason, a broken jobs journal with the file name only; ``public_detail`` also
reduces ``file:`` URIs (their scheme in any case), ``~`` paths and a path directly after a ``:`` to
their last component. An exception no route maps (a bug, an I/O failure) answers 500 with
``{"detail": INTERNAL_ERROR}``: never its message, a path or a traceback. Every route declares that
500 as ``ApiError``. The 422 of ``GET /reports/{kind}/{report_id}`` is declared as
``ApiError | HTTPValidationError`` (the store's refusal, or FastAPI's request validation of an
unknown ``kind``). ``/health``, ``/healthz``, ``/readyz``, ``/contracts`` and
``/lifecycle/transitions`` have named response models (``Health``, ``ContractNames``,
``LifecycleTransition``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, RootModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.store import (
    InvalidReportId,
    ReportEnvelope,
    ReportKind,
    ReportListing,
    ReportMalformed,
    ReportNotFound,
    ReportStore,
)
from apps.worker.jobs import JobHistory, JobRecord, JobStatus, read_job_results
from apps.worker.journal import JournalCorrupted
from core.contracts.knowledge import (
    KnowledgeProvider,
    KnowledgeProviderError,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.contracts.registry import CONTRACT_MODELS
from core.lifecycle.strategy import ALLOWED_TRANSITIONS

__all__ = [
    "API_VERSION",
    "INTERNAL_ERROR",
    "JOBS_NOT_CONFIGURED",
    "KNOWLEDGE_NOT_CONFIGURED",
    "ApiError",
    "ContractNames",
    "Health",
    "JobList",
    "JobView",
    "LifecycleTransition",
    "create_app",
    "public_detail",
]

API_VERSION = "0.1.0"

#: Stable ``detail`` of the 503 answers (clients may match on them).
KNOWLEDGE_NOT_CONFIGURED: Final = "no knowledge provider is configured"
JOBS_NOT_CONFIGURED: Final = "no job results journal is configured"
#: Stable ``detail`` of the catch-all 500 (an exception no route maps; nothing else is disclosed).
INTERNAL_ERROR: Final = "internal server error"

_JOB_ID = re.compile(r"^[0-9a-f]{64}$")

#: An absolute POSIX path inside an error message, as the named group ``path``, preceded by:
#:
#: - a ``file:`` URI's scheme and authority (``file:/p``, ``file:///p``, ``file://host/p``); the
#:   scheme matches in any case (``FILE:``, ``FiLe:``; RFC 3986 §3.1), authority / path as above;
#: - any other ``:`` directly followed by the path or by an empty authority (``x:/p``, ``x:///p``)
#:   -- a URL's ``://host/...`` is not a path (its first ``/`` is followed by another ``/``);
#: - a home-relative ``~`` / ``~user`` (``~/p``);
#: - otherwise nothing: the first ``/`` must not follow a word character, ``.``, ``~``, ``:``,
#:   ``-`` or another ``/`` (not the ``//`` or ``/v/...`` of a URL, nor a ratio ``1/2``).
_ABSOLUTE_PATH = re.compile(
    r"(?:\b(?i:file):(?://[^/\s'\"(),;]*)?"
    r"|(?<=:)(?://)?"
    r"|(?<![\w.~:/-])~[\w.-]*"
    r"|(?<![\w.~:/-]))"
    r"(?P<path>(?:/[^\s/'\"(),;]+)+/?)"
)


def public_detail(detail: str) -> str:
    """``detail`` with every absolute filesystem path reduced to its last component.

    Applied to every error body this API answers (its ``HTTPException`` handler), so a message
    built from an ``OSError`` or a store / journal error never discloses the server's layout. A
    ``file:`` URI's scheme / authority and a ``~`` prefix are dropped with the path (only its last
    component is kept)."""
    return _ABSOLUTE_PATH.sub(
        lambda match: match.group("path").rstrip("/").rsplit("/", 1)[-1], detail
    )


class ApiError(BaseModel):
    """The error body of every status this API answers itself (400/404/422/500/502/503).

    Includes the catch-all 500 of an unexpected exception, whose ``detail`` is always
    "internal server error" (no message, path or traceback)."""

    detail: str


class Health(BaseModel):
    """Liveness (``/health`` and ``/healthz``) or readiness (``/readyz``) response."""

    model_config = ConfigDict(frozen=True)

    status: str
    api_version: str


class ContractNames(RootModel[list[str]]):
    """``GET /contracts``: the registered contract model names, sorted."""


class LifecycleTransition(BaseModel):
    """One allowed lifecycle transition (``GET /lifecycle/transitions``), ``{"from", "to"}``."""

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    from_: str = Field(alias="from")
    to: str


class JobView(BaseModel):
    """One job of the worker's results journal (``apps.worker.jobs.JobRecord``), read-only.

    ``status`` is ``succeeded``, ``failed`` or ``interrupted`` (started without a recorded result:
    running, or died and awaiting review / an idempotent re-run). ``attempts`` / ``result`` /
    ``error`` come from the ``job_result`` line and are ``null`` while interrupted.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    name: str
    params: Any
    status: JobStatus
    attempts: int | None
    result: Any
    error: str | None
    starts: int
    reruns: int
    first_seq: int
    last_seq: int


class JobList(BaseModel):
    """``GET /jobs``: every job in the order of its first start, plus the verified chain tip."""

    model_config = ConfigDict(frozen=True)

    jobs: list[JobView]
    head_hash: str
    lines: int


def _job_view(record: JobRecord) -> JobView:
    outcome = record.outcome
    return JobView(
        job_id=record.job_id,
        name=record.name,
        params=record.params,
        status=record.status,
        attempts=None if outcome is None else outcome.attempts,
        result=None if outcome is None else outcome.result,
        error=None if outcome is None else outcome.error,
        starts=record.starts,
        reruns=record.reruns,
        first_seq=record.first_seq,
        last_seq=record.last_seq,
    )


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """``ApiError`` responses for ``codes``, plus the catch-all 500 every route can answer."""
    return {code: {"model": ApiError} for code in sorted({*codes, 500})}


#: ``GET /reports/{kind}/{report_id}``'s 422: the store's refusal (``ApiError``) or FastAPI's own
#: request validation (its ``HTTPValidationError`` component, which every other route with a
#: parameter still registers). A route that declares a 422 replaces FastAPI's default entry, so
#: both shapes are declared here by reference (a model of our own named ``HTTPValidationError``
#: would collide with FastAPI's component).
_MALFORMED_REPORT: Final[dict[str, Any]] = {
    "description": "The report file is malformed or fails its kind's contract / identity check "
    "(ApiError), or the request is invalid (HTTPValidationError)",
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/ApiError"},
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                ]
            }
        }
    },
}


def create_app(
    *,
    knowledge: KnowledgeProvider | None = None,
    reports_root: Path | None = None,
    jobs_results: Path | None = None,
    jobs_idempotent: Collection[str] = (),
) -> FastAPI:
    """``jobs_results``: the worker's durable results journal (``JobRunner(results=...)``);
    ``jobs_idempotent``: the handler names that runner declares idempotent (its ``job_rerun``
    lines are refused otherwise, exactly as on the runner's own reopening).

    Deployment note: ``jobs_idempotent`` **must equal** the runner's ``idempotent=``. A name the
    runner declares but the API does not makes every ``/jobs`` call a 500 as soon as the journal
    holds one ``job_rerun`` of that handler (the replay refuses an undeclared re-run, fail
    closed); there is no handler table here to check the names against. The journal is re-read
    and re-verified on every request (no cache); a read racing the runner's append can see a
    partial trailing line and answer 500 once -- retrying reads the completed line."""
    if isinstance(jobs_idempotent, str | bytes):
        raise TypeError("jobs_idempotent must be a collection of handler names, not one string")
    app = FastAPI(title="HLENS-AutoResearch API", version=API_VERSION)

    @app.exception_handler(StarletteHTTPException)
    async def _path_free_errors(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = public_detail(exc.detail) if isinstance(exc.detail, str) else exc.detail
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        # the documented ApiError shape; the exception's message may name a server path and its
        # traceback the code layout, so neither is answered (the server log keeps both)
        return JSONResponse({"detail": INTERNAL_ERROR}, status_code=500)

    reports = ReportStore(reports_root)
    idempotent = frozenset(jobs_idempotent)

    @app.get("/health", response_model=Health, responses=_errors())
    def health() -> Health:
        return Health(status="ok", api_version=API_VERSION)

    @app.get("/healthz", response_model=Health, responses=_errors())
    def healthz() -> Health:
        return Health(status="ok", api_version=API_VERSION)

    @app.get("/readyz", response_model=Health, responses=_errors(503))
    def readyz() -> Health:
        """Configured read sources must answer before the API is reported ready."""
        if knowledge is not None:
            try:
                knowledge.search(KnowledgeQuery(terms=("__readiness_check__",), limit=1))
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail="configured knowledge source is unavailable"
                ) from exc

        if reports_root is not None:
            try:
                with os.scandir(reports_root) as entries:
                    next(entries, None)
            except OSError as exc:
                raise HTTPException(
                    status_code=503, detail="configured reports source is unavailable"
                ) from exc

        if jobs_results is not None:
            try:
                _job_history()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail="configured jobs source is unavailable"
                ) from exc

        return Health(status="ready", api_version=API_VERSION)

    @app.get("/contracts", response_model=ContractNames, responses=_errors())
    def contracts() -> ContractNames:
        return ContractNames(sorted(model.__name__ for model in CONTRACT_MODELS))

    @app.get(
        "/lifecycle/transitions", response_model=list[LifecycleTransition], responses=_errors()
    )
    def transitions() -> list[LifecycleTransition]:
        return [
            LifecycleTransition.model_validate({"from": source.value, "to": target.value})
            for source, target in sorted(ALLOWED_TRANSITIONS, key=lambda pair: (pair[0], pair[1]))
        ]

    @app.post("/knowledge/search", response_model=KnowledgeResult, responses=_errors(502, 503))
    def knowledge_search(query: KnowledgeQuery) -> KnowledgeResult:
        if knowledge is None:
            raise HTTPException(status_code=503, detail=KNOWLEDGE_NOT_CONFIGURED)
        try:
            return knowledge.search(query)
        except KnowledgeProviderError as exc:  # fail closed: the provider cannot answer honestly
            raise HTTPException(
                status_code=502, detail=f"knowledge provider could not answer: {exc}"
            ) from exc

    @app.get("/reports/{kind}", response_model=ReportListing, responses=_errors())
    def list_reports(kind: ReportKind) -> ReportListing:
        return reports.listing(kind)

    @app.get(
        "/reports/{kind}/{report_id}",
        response_model=ReportEnvelope,
        responses={
            **_errors(400, 404),
            422: _MALFORMED_REPORT,
        },
    )
    def get_report(kind: ReportKind, report_id: str) -> ReportEnvelope:
        try:
            return reports.get(kind, report_id)
        except InvalidReportId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ReportNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReportMalformed as exc:  # the reason only: never the server's file path
            raise HTTPException(
                status_code=422, detail=f"{kind.value}/{report_id} is malformed: {exc.reason}"
            ) from exc

    def _job_history() -> JobHistory:
        if jobs_results is None:
            raise HTTPException(status_code=503, detail=JOBS_NOT_CONFIGURED)
        try:
            return read_job_results(jobs_results, idempotent=idempotent)
        except JournalCorrupted as exc:  # includes JobResultsCorrupted; never partial data
            # the reason without the server's filesystem path (as the report listing does); the
            # error handler reduces any other absolute path to its last component
            reason = str(exc).replace(str(jobs_results), Path(jobs_results).name)
            raise HTTPException(
                status_code=500, detail=f"job results journal failed verification: {reason}"
            ) from exc

    @app.get("/jobs", response_model=JobList, responses=_errors(500, 503))
    def list_jobs() -> JobList:
        history = _job_history()
        return JobList(
            jobs=[_job_view(record) for record in history.jobs],
            head_hash=history.head_hash,
            lines=history.lines,
        )

    @app.get("/jobs/{job_id}", response_model=JobView, responses=_errors(400, 404, 500, 503))
    def get_job(job_id: str) -> JobView:
        if not _JOB_ID.fullmatch(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job id: {job_id!r}")
        for record in _job_history().jobs:
            if record.job_id == job_id:
                return _job_view(record)
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    return app
