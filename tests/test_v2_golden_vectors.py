"""v2.0.0 golden vectors (ADR-0052 §4 prerequisite; `tests/golden/v2_0_0/README.md`).

Every recorded 2.0.0 payload must validate with the current models **as its recorded version**
(the envelope `schema_version` is kept, never rewritten), re-serialize byte-identically and hash
to the recorded content hash. The files are generated once and never regenerated (H6).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import RevisionRecord
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Contract, canonical_json
from core.domain.research import GateResult, ValidationReport

GOLDEN = Path(__file__).resolve().parent / "golden" / "v2_0_0"
MODELS: dict[str, type[Contract]] = {
    "GateResult": GateResult,
    "ValidationReport": ValidationReport,
    "ValidationProfile": ValidationProfile,
    "RevisionRecord": RevisionRecord,
}
#: The files and their recorded hashes (pinned here too, so a regenerated file cannot pass).
EXPECTED: dict[str, str] = {
    "gate_result.json": "9932e55ade5a2b91c22f7af10b0c1aa2a114c9c6a5e6c5433b2b885e90a94686",
    "gate_result_no_threshold.json": (
        "43f29c8f603816c04fd42543f5053b5535eb69ae25c7c592eb63360f2295777a"
    ),
    "validation_report.json": "d0fe99decd1f225e588def01724ded1c4677c751c704265682e0c9bcda57c3aa",
    "validation_profile.json": "0ca8401aaf4471dd8a032dfe70f0d39acda4ee4a5d86603ca8e8e0510dc36fef",
    "validation_profile_frozen.json": (
        "4ccd172282cf13f0c44c2166cf326585d91cb8a91aad48b5afe7d768bf688baf"
    ),
    "revision_record.json": "7e264e4d6ddcec47cf64bb2ab5e14642edf5b96e0fad145c4b2afa6f4a68ccd1",
}


def _load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    return document


def test_the_golden_set_is_exactly_the_pinned_files() -> None:
    present = {path.name for path in GOLDEN.glob("*.json")}
    assert present == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recorded_2_0_0_payload_reads_and_hashes_as_recorded(name: str) -> None:
    document = _load(name)
    assert document["content_hash"] == EXPECTED[name]
    assert document["contract_schema_version"] == "2.0.0"
    payload = document["payload"]
    assert payload["schema_version"] == "2.0.0"
    model = MODELS[document["model"]]
    for obj in (model.model_validate(payload), model.model_validate_json(json.dumps(payload))):
        assert obj.schema_version == "2.0.0"  # read as its recorded version, never rewritten
        assert obj.model_dump(mode="json") == payload
        assert obj.content_hash() == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_recorded_hash_is_the_documented_rule(name: str) -> None:
    """`content_hash` = SHA-256 of the canonical JSON minus the model's non-semantic fields."""
    document = _load(name)
    model = MODELS[document["model"]]
    semantic = {
        key: value
        for key, value in document["payload"].items()
        if key not in model._non_semantic_fields()
    }
    digest = hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
    assert digest == EXPECTED[name]


def test_a_changed_payload_does_not_keep_the_recorded_hash() -> None:
    """Negative control: the pins are sensitive to content."""
    document = _load("gate_result.json")
    changed = GateResult.model_validate({**document["payload"], "value": 0.5})
    assert changed.content_hash() != EXPECTED["gate_result.json"]
