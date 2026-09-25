"""ADR-0048: the API skeleton answers, and its committed OpenAPI document is current."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.openapi import TARGET
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
