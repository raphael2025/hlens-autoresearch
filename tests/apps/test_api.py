"""ADR-0048: the API skeleton answers, and its committed OpenAPI document is current."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.app import API_VERSION, KNOWLEDGE_NOT_CONFIGURED, public_detail
from apps.api.openapi import TARGET
from core.contracts.knowledge import (
    KnowledgeProviderDescriptor,
    KnowledgeProviderError,
    KnowledgeQuery,
    KnowledgeResult,
)
from plugins.knowledge import LocalKnowledgeProvider


def test_health_contracts_and_transitions() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json()["status"] == "ok"
    assert "KnowledgeQuery" in client.get("/contracts").json()
    assert {"from": "IDEA", "to": "CANDIDATE"} in client.get("/lifecycle/transitions").json()


def test_knowledge_search_goes_through_the_provider() -> None:
    client = TestClient(create_app(knowledge=LocalKnowledgeProvider()))
    body = client.post("/knowledge/search", json={"terms": ["momentum"], "limit": 10}).json()
    assert {item["name"] for item in body["items"]} >= {"strategy_time_series_momentum"}
    assert client.post("/knowledge/search", json={"limit": 0}).status_code == 422


def test_the_committed_openapi_document_is_current() -> None:
    committed = json.loads(Path(TARGET).read_text(encoding="utf-8"))
    assert committed == create_app().openapi()


# --- knowledge search error mapping (2026-09-26) ------------------------------------------


class _FailingProvider:
    """A provider that cannot answer honestly (e.g. an unreadable item source)."""

    descriptor = KnowledgeProviderDescriptor(
        name="failing", version="1.0.0", deterministic=True, sources=("file:none",)
    )

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        raise KnowledgeProviderError("items.json: unreadable item file")


def test_knowledge_search_without_a_provider_is_a_503_with_a_stable_body() -> None:
    response = TestClient(create_app()).post("/knowledge/search", json={"terms": ["x"]})
    assert response.status_code == 503
    assert response.json() == {"detail": KNOWLEDGE_NOT_CONFIGURED}


def test_a_knowledge_provider_error_is_a_502_never_a_200() -> None:
    client = TestClient(create_app(knowledge=_FailingProvider()))
    response = client.post("/knowledge/search", json={"terms": ["x"]})
    assert response.status_code == 502
    body = response.json()
    assert set(body) == {"detail"} and "unreadable item file" in body["detail"]


def test_knowledge_search_success_matches_the_result_contract() -> None:
    client = TestClient(create_app(knowledge=LocalKnowledgeProvider()))
    response = client.post("/knowledge/search", json={"terms": ["momentum"], "limit": 10})
    assert response.status_code == 200
    result = KnowledgeResult.model_validate(response.json())  # the provenance/hash invariants
    assert result.items and "error" not in response.json()


def test_the_openapi_document_types_knowledge_and_the_error_statuses() -> None:
    spec = create_app().openapi()
    search = spec["paths"]["/knowledge/search"]["post"]["responses"]
    assert search["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/KnowledgeResult"
    }
    assert {"502", "503"} <= set(search)
    assert search["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiError"
    }
    assert "/jobs" in spec["paths"] and "/jobs/{job_id}" in spec["paths"]
    listing = spec["paths"]["/reports/{kind}"]["get"]["responses"]["200"]
    assert listing["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReportListing"
    }


def test_the_api_has_no_mutation_endpoints_besides_the_read_only_search() -> None:
    """ADR-0048: read-only. The one POST is the knowledge search (a query body, no writes)."""
    methods = {
        (path, method) for path, item in create_app().openapi()["paths"].items() for method in item
    }
    writes = {(p, m) for p, m in methods if m in {"post", "put", "patch", "delete"}}
    assert writes == {("/knowledge/search", "post")}


# --- response models, the report 422 and path-free error bodies (2026-09-26) ---------------


def _ok_schema(spec: dict[str, Any], path: str) -> Any:
    return spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_health_contracts_and_transitions_have_named_response_models() -> None:
    spec = create_app().openapi()
    assert _ok_schema(spec, "/health") == {"$ref": "#/components/schemas/Health"}
    assert _ok_schema(spec, "/contracts") == {"$ref": "#/components/schemas/ContractNames"}
    transitions = _ok_schema(spec, "/lifecycle/transitions")
    assert transitions["items"] == {"$ref": "#/components/schemas/LifecycleTransition"}
    schemas = spec["components"]["schemas"]
    assert set(schemas["Health"]["required"]) == {"status", "api_version"}
    assert schemas["ContractNames"]["type"] == "array"
    assert set(schemas["LifecycleTransition"]["properties"]) == {"from", "to"}


def test_the_typed_endpoints_answer_exactly_the_same_json() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok", "api_version": API_VERSION}
    names = client.get("/contracts").json()
    assert names == sorted(names) and all(isinstance(name, str) for name in names)
    transitions = client.get("/lifecycle/transitions").json()
    assert transitions and all(set(item) == {"from", "to"} for item in transitions)


def test_the_report_detail_422_is_declared_as_api_error_or_request_validation() -> None:
    spec = create_app().openapi()
    responses = spec["paths"]["/reports/{kind}/{report_id}"]["get"]["responses"]
    assert responses["422"]["content"]["application/json"]["schema"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ApiError"},
            {"$ref": "#/components/schemas/HTTPValidationError"},
        ]
    }
    # both referenced components exist (FastAPI's own is registered by the other routes)
    assert {"ApiError", "HTTPValidationError"} <= set(spec["components"]["schemas"])
    # and the request-validation shape really is answered there (an unknown kind)
    assert isinstance(TestClient(create_app()).get("/reports/nope/x").json()["detail"], list)


def test_public_detail_reduces_absolute_paths_to_their_last_component() -> None:
    assert public_detail("[Errno 2] No such file: '/srv/data/items.json'") == (
        "[Errno 2] No such file: 'items.json'"
    )
    assert public_detail("/tmp/r/jobs.jsonl:3 is a blank line") == "jobs.jsonl:3 is a blank line"
    assert public_detail("bad (/var/x/y.json) file") == "bad (y.json) file"
    for not_a_path in (
        "validation_report/r1 not found",
        "see https://errors.pydantic.dev/2.13/v/value_error",
        "ratio 1/2",
    ):
        assert public_detail(not_a_path) == not_a_path


class _PathLeakingProvider(_FailingProvider):
    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        raise KnowledgeProviderError("items.json: unreadable: [Errno 13] '/srv/k/items.json'")


def test_no_error_body_carries_a_server_path() -> None:
    response = TestClient(create_app(knowledge=_PathLeakingProvider())).post(
        "/knowledge/search", json={"terms": ["x"]}
    )
    assert response.status_code == 502
    assert "/srv/k" not in response.text
    assert response.json()["detail"].endswith("[Errno 13] 'items.json'")
