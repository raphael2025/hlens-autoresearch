"""FastAPI application (ADR-0048; framework, FRAMEWORK_IMPLEMENTED / NOT_VALIDATED).

Read-only endpoints over the domain and plugins; no business rules live here, and nothing imports
``research/`` (01-system.md §3). Providers are injected, so tests and deployments choose them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from apps.api.store import (
    InvalidReportId,
    ReportEnvelope,
    ReportKind,
    ReportMalformed,
    ReportNotFound,
    ReportStore,
)
from core.contracts.knowledge import KnowledgeProvider, KnowledgeQuery, KnowledgeResult
from core.contracts.registry import CONTRACT_MODELS
from core.lifecycle.strategy import ALLOWED_TRANSITIONS

__all__ = ["API_VERSION", "create_app"]

API_VERSION = "0.1.0"


def create_app(
    *,
    knowledge: KnowledgeProvider | None = None,
    reports_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="HLENS-AutoResearch API", version=API_VERSION)
    reports = ReportStore(reports_root)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "api_version": API_VERSION}

    @app.get("/contracts")
    def contracts() -> list[str]:
        return sorted(model.__name__ for model in CONTRACT_MODELS)

    @app.get("/lifecycle/transitions")
    def transitions() -> list[dict[str, str]]:
        return [
            {"from": source.value, "to": target.value}
            for source, target in sorted(ALLOWED_TRANSITIONS, key=lambda pair: (pair[0], pair[1]))
        ]

    @app.post("/knowledge/search", response_model=None)
    def knowledge_search(query: KnowledgeQuery) -> dict[str, Any]:
        if knowledge is None:
            return {"error": "no knowledge provider is configured"}
        result: KnowledgeResult = knowledge.search(query)
        return result.model_dump(mode="json")

    @app.get("/reports/{kind}", response_model=list[ReportEnvelope])
    def list_reports(kind: ReportKind) -> list[ReportEnvelope]:
        return reports.list(kind)

    @app.get("/reports/{kind}/{report_id}", response_model=ReportEnvelope)
    def get_report(kind: ReportKind, report_id: str) -> ReportEnvelope:
        try:
            return reports.get(kind, report_id)
        except InvalidReportId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ReportNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReportMalformed as exc:  # e.g. an edited research_loop_round (ADR-0050)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app
