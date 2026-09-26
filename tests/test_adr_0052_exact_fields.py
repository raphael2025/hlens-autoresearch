"""ADR-0052 §1 ~ §3 contract fields at 2.1.0 (option B): exact decimals, Profile fields, D-CTRL.

- ``ExactDecimal``: one canonical text per number; float / NaN / ±Infinity / non-canonical text
  refused.
- ``GateResult.value_exact`` / ``threshold_exact``: the float is derived (a mismatch is refused),
  an exact threshold needs an exact value, and when exact values are present the derived floats
  are left out of the content-hash payload (the wire keeps them); absent, they are omitted from
  the payload, so 2.0.0 payloads and hashes are unchanged (``tests/test_v2_golden_vectors.py``).
- Profile ``*_exact`` siblings and the §2 / §3 fields: structural ranges only. **Every number in
  this file is TEST ONLY** — none is a Profile recommendation (ADR-0007 two-step freeze).
- No ADR-0052 field under a 2.0.0 envelope (ADR-0052 §4, Codex K3).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.validation_profile import CapacityParams, CrossAssetParams, ValidationProfile
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    EXACT_DECIMAL_PATTERN,
    canonical_json,
    exact_decimal,
    exact_decimal_text,
)
from core.domain.research import GateResult, ValidationReport, Verdict

GOLDEN = Path(__file__).resolve().parent / "golden" / "v2_0_0"


def _payload(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    payload: dict[str, Any] = document["payload"]
    return payload


def _without_envelopes(value: Any) -> Any:
    """The same payload with every ``schema_version`` dropped: built at the current version."""
    if isinstance(value, dict):
        return {k: _without_envelopes(v) for k, v in value.items() if k != "schema_version"}
    if isinstance(value, list):
        return [_without_envelopes(item) for item in value]
    return value


def _hash_of(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ ExactDecimal


@pytest.mark.parametrize(
    ("given", "text"),
    [
        (Decimal("0.0500"), "0.05"),
        (Decimal("-0.000"), "0"),
        (Decimal("1E+3"), "1000"),
        (7, "7"),
        ("0.05", "0.05"),
        ("-12.5", "-12.5"),
        ("0", "0"),
    ],
)
def test_exact_decimal_has_one_canonical_text(given: object, text: str) -> None:
    value = exact_decimal(given)
    assert exact_decimal_text(value) == text


@pytest.mark.parametrize(
    "given",
    [0.05, float("nan"), Decimal("NaN"), Decimal("Infinity"), True, "1.0", "01", "-0", "1e3", " 1"],
)
def test_exact_decimal_refuses_floats_and_non_canonical_text(given: object) -> None:
    with pytest.raises(ValueError):
        exact_decimal(given)


def test_exact_decimal_pattern_is_the_wire_rule() -> None:
    import re

    assert re.fullmatch(EXACT_DECIMAL_PATTERN, "0.05")
    assert not re.fullmatch(EXACT_DECIMAL_PATTERN, "0.050")


# ------------------------------------------------------------------ GateResult


def _gate(**changes: Any) -> GateResult:
    fields: dict[str, Any] = {
        "gate_id": "G3.adjusted_p_value",
        "metric": "p[at_most]",
        "value": 0.03,
        "threshold": 0.05,
        "threshold_source": "significance.multiple_testing_threshold_exact",
        "verdict": Verdict.PASS,
        "value_exact": "0.03",
        "threshold_exact": "0.05",
    }
    fields.update(changes)
    return GateResult(**fields)


def test_a_gate_with_exact_values_derives_its_floats_and_hashes_the_exact_ones() -> None:
    gate = _gate()
    assert gate.schema_version == CONTRACT_SCHEMA_VERSION
    assert (gate.value_exact, gate.threshold_exact) == (Decimal("0.03"), Decimal("0.05"))
    wire = gate.model_dump(mode="json")
    assert wire["value"] == 0.03 and wire["value_exact"] == "0.03"  # the wire keeps the floats
    semantic = {k: v for k, v in wire.items() if k not in ("value", "threshold", "created_at")}
    assert gate.content_hash() == _hash_of(semantic)  # identity: exact values only


def test_a_gate_without_exact_values_is_unchanged() -> None:
    gate = _gate(value_exact=None, threshold_exact=None)
    wire = gate.model_dump(mode="json")
    assert "value_exact" not in wire and "threshold_exact" not in wire
    assert gate.content_hash() == _hash_of(wire)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"value": 0.031}, "value_exact"),
        ({"threshold": 0.051}, "threshold_exact"),
        ({"value_exact": None}, "threshold_exact 需要 value_exact"),
        ({"threshold_exact": None}, "threshold_exact"),
        ({"value_exact": 0.03}, "浮点"),
        ({"schema_version": "2.0.0"}, "2.1.0"),
    ],
)
def test_inconsistent_or_misversioned_exact_gates_are_refused(
    changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        _gate(**changes)


def test_a_report_hashes_its_gates_by_their_exact_values() -> None:
    payload = _without_envelopes(_payload("validation_report.json"))
    report = ValidationReport.model_validate({**payload, "gates": [_gate().model_dump()]})
    wire = report.model_dump(mode="json")
    assert wire["gates"][0]["value"] == 0.03
    semantic = {k: v for k, v in wire.items() if k != "created_at"}
    semantic["gates"] = [
        {k: v for k, v in gate.items() if k not in ("value", "threshold")}
        for gate in semantic["gates"]
    ]
    assert report.content_hash() == _hash_of(semantic)


# ------------------------------------------------------------------ Validation Profile


def _profile(**sections: dict[str, Any]) -> ValidationProfile:
    """The golden profile rebuilt at the current version, sections updated (TEST ONLY)."""
    payload = _without_envelopes(_payload("validation_profile.json"))
    for name, changes in sections.items():
        payload[name] = {**payload[name], **changes} if name in payload else changes
    return ValidationProfile.model_validate(payload)


def test_an_old_profile_payload_reads_as_2_0_0_without_the_new_fields() -> None:
    old = ValidationProfile.model_validate(_payload("validation_profile.json"))
    assert old.schema_version == "2.0.0"
    assert old.capacity is None and old.cross_asset is None
    assert old.significance.negative_control_threshold is None
    assert old.significance.cscv_partitions is None


def test_a_2_1_0_profile_carries_every_adr_0052_field() -> None:  # TEST ONLY numbers
    profile = _profile(
        significance={
            "multiple_testing_threshold_exact": "0.5",
            "overfitting_threshold_exact": "0.5",
            "cscv_partitions": 8,
            "negative_control_threshold": "0.2",
        },
        capacity={
            "min_capacity": "1000",
            "max_participation_rate": "0.1",
            "impact_coefficient": "0",
            "impact_model": "square_root",
        },
        cross_asset={"min_positive_fraction": "0.5"},
        data_split={"sealed_oos_max_unsealings": 2},
        sample_size={"max_undersampled_pnl_share": "0.3"},
        cost_stress={"stress_multipliers_exact": ["2"], "min_breakeven_cost_multiple_exact": "2"},
        lifecycle={"degradation_thresholds_exact": {"sharpe_drop": "0.5"}},
        inconclusive_bands_exact={"G3.adjusted_p_value": "0.01"},
    )
    assert profile.schema_version == CONTRACT_SCHEMA_VERSION
    assert profile.significance.negative_control_threshold == Decimal("0.2")
    wire = profile.model_dump(mode="json")
    assert wire["significance"]["multiple_testing_threshold"] == 0.5  # derived floats kept
    semantic = json.loads(profile._semantic_canonical_json())
    assert "multiple_testing_threshold" not in semantic["significance"]  # hashed exactly
    assert "inconclusive_bands" not in semantic and "inconclusive_bands_exact" in semantic


@pytest.mark.parametrize(
    ("sections", "match"),
    [
        ({"significance": {"cscv_partitions": 3}}, "偶数"),
        ({"significance": {"cscv_partitions": 0}}, "偶数"),
        ({"significance": {"negative_control_threshold": "1.5"}}, "<= 1"),
        ({"significance": {"multiple_testing_threshold_exact": "0.4"}}, "派生"),
        ({"capacity": {"min_capacity": "0"}}, "> 0"),
        ({"capacity": {"max_participation_rate": "0"}}, "> 0"),
        ({"capacity": {"impact_coefficient": "-1"}}, ">= 0"),
        ({"capacity": {}}, "至少"),
        ({"cross_asset": {"min_positive_fraction": "1.01"}}, "<= 1"),
        ({"data_split": {"sealed_oos_max_unsealings": 0}}, "greater than 0"),
        ({"sample_size": {"max_undersampled_pnl_share": "-0.1"}}, ">= 0"),
        ({"lifecycle": {"degradation_thresholds_exact": {"other": "0.5"}}}, "派生"),
        ({"cost_stress": {"stress_multipliers_exact": ["2", "3"]}}, "派生"),
    ],
)
def test_structural_ranges_and_derivation_are_enforced(
    sections: dict[str, dict[str, Any]], match: str
) -> None:  # TEST ONLY numbers
    with pytest.raises(ValidationError, match=match):
        _profile(**sections)


def test_no_adr_0052_field_or_model_under_a_2_0_0_envelope() -> None:
    payload = _payload("validation_profile.json")
    with pytest.raises(ValidationError, match="2.1.0"):
        ValidationProfile.model_validate(
            {**payload, "significance": {**payload["significance"], "cscv_partitions": 8}}
        )
    with pytest.raises(ValidationError, match="2.1.0"):
        CapacityParams(schema_version="2.0.0", min_capacity=Decimal(1))
    with pytest.raises(ValidationError, match="2.1.0"):
        CrossAssetParams(schema_version="2.0.0", min_positive_fraction=Decimal("0.5"))
    assert CrossAssetParams(min_positive_fraction=Decimal("0.5")).schema_version == "2.1.0"
