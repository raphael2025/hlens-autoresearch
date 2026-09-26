"""The committed Phase 9 calibration evidence (evidence only — not a Profile decision).

Cheap checks only: the evidence setups are well-formed and deterministic (``inputs_payload``
hashes pinned), and the committed reports under ``docs/research/calibration/`` parse, carry a
``report_hash`` equal to the hash of their content, and were produced from exactly these setups
(their ``inputs`` equal the setups' ``inputs_payload``). Regenerating the reports runs the full
pipeline over thousands of markets (tens of minutes) and is a documented manual command
(``docs/research/calibration/README.md``), not a test; a reduced-seed regeneration would not
reproduce the committed bytes and is deliberately not attempted.

Recorded version (ADR-0055): the reports and the ``INPUTS_HASHES`` pins were produced by the
contract 2.1.0 code, and a setup's ``inputs_payload`` embeds envelopes and content hashes of its
Profiles / specs. They are therefore checked against the setups built by the 2.1.0 code — a fresh
interpreter that imports the setup modules inside ``contract_schema_version_scope("2.1.0")`` (as
``regenerate_legacy`` does for the console fixtures) — never regenerated or re-pinned. The setups
built by the current code must still be deterministic and differ from the recorded ones only by
their envelopes and the content hashes taken over them (so the same seeds, effects, candidates and
parameters).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.domain.base import CONTRACT_SCHEMA_VERSION, content_hash
from research.synthetic_lab.gate_calibration import (
    DISCLAIMER,
    GateCalibrationSetup,
    MultiInstrumentCalibrationSetup,
)
from tests.research.synthetic_lab import evidence_setups as ev
from tests.research.synthetic_lab import gate_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
CALIBRATION_DIR = REPO_ROOT / "docs" / "research" / "calibration"
#: The contract version whose code produced the committed reports and the pins below.
RECORDED_VERSION = "2.1.0"

AnySetup = GateCalibrationSetup | MultiInstrumentCalibrationSetup

#: Pinned ``content_hash(setup.inputs_payload())``: any change to seeds, effects, candidates,
#: base spec, detector description or alpha changes these (and invalidates the committed reports).
INPUTS_HASHES: dict[str, str] = {
    "single_instrument_evidence": (
        "c8378376586dc1c09b066b5a32ffaef7aadd50eb6c25587241412f5f12398659"
    ),
    "multi_instrument_evidence": (
        "97539f0d73b4ffbc232e26a1742f8444d0596cfdc8dcd241257680df6ae076fc"
    ),
    "single_instrument_probe": "947aab36fe0ec6518b283f0b1563dc080b56c63c79be0128e57224422b0f1c27",
    "multi_instrument_probe": "73b8eb9c156217ad573469738634890d81cd53d74263dac8d747dbba1bfca33e",
}

#: The committed reports (file stem = the factory that produced it) and their pinned hashes.
REPORT_HASHES: dict[str, str] = {
    "single_instrument_evidence": (
        "bada61369ed52c29eaaba1efffbf610f768fb325ee3a56e3f74fa56a33de4841"
    ),
    "multi_instrument_evidence": "0f04d1b649d7ce6357e47d84e6ae709d343bfcbb44271398a34cb2aa83f2cecd",
}

FACTORIES: dict[str, Callable[[], AnySetup]] = {
    "single_instrument_evidence": ev.single_instrument_evidence,
    "multi_instrument_evidence": ev.multi_instrument_evidence,
    "single_instrument_probe": ev.single_instrument_probe,
    "multi_instrument_probe": ev.multi_instrument_probe,
}


_RECORDED_PROGRAM = """\
import json, sys
from core.domain.base import contract_schema_version_scope
version = sys.argv[1]
with contract_schema_version_scope(version):
    from tests.research.synthetic_lab import evidence_setups as ev
    names = sys.argv[2:]
    print(json.dumps({name: getattr(ev, name)().inputs_payload() for name in names}))
"""


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    """Every setup's ``inputs_payload`` as the ``RECORDED_VERSION`` code builds it (JSON form)."""
    result = subprocess.run(
        [sys.executable, "-c", _RECORDED_PROGRAM, RECORDED_VERSION, *sorted(FACTORIES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    payloads: dict[str, Any] = json.loads(lines[-1])
    return payloads


def _envelopes(value: object) -> list[str]:
    if isinstance(value, dict):
        own = [value["schema_version"]] if "schema_version" in value else []
        return own + [v for item in value.values() for v in _envelopes(item)]
    if isinstance(value, list):
        return [v for item in value for v in _envelopes(item)]
    return []


def _without_envelopes(value: object) -> object:
    """``value`` without envelopes and the ``*_hash`` values taken over enveloped objects."""
    if isinstance(value, dict):
        return {
            key: _without_envelopes(item)
            for key, item in value.items()
            if key != "schema_version" and not key.endswith("_hash")
        }
    if isinstance(value, list):
        return [_without_envelopes(item) for item in value]
    return value


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_the_current_setups_are_the_recorded_ones_but_for_envelopes(
    name: str, recorded: dict[str, Any]
) -> None:
    current = json.loads(json.dumps(FACTORIES[name]().inputs_payload()))
    assert current != recorded[name]  # the current envelope is part of the payload
    assert set(_envelopes(current)) == {CONTRACT_SCHEMA_VERSION}
    assert _without_envelopes(current) == _without_envelopes(recorded[name])


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_the_evidence_setups_are_deterministic(name: str, recorded: dict[str, Any]) -> None:
    first, second = FACTORIES[name](), FACTORIES[name]()
    assert first.inputs_payload() == second.inputs_payload()
    # the pins describe what the recorded-version code builds (see module docs)
    assert content_hash(recorded[name]) == INPUTS_HASHES[name]
    assert set(_envelopes(recorded[name])) == {RECORDED_VERSION}


def test_the_candidates_are_only_the_test_only_fixtures() -> None:
    for factory in FACTORIES.values():
        setup = factory()
        assert setup.candidates == (fx.LAX_TEST_ONLY_PROFILE, fx.STRICT_TEST_ONLY_PROFILE)
        assert all(profile.name.startswith("test_only_") for profile in setup.candidates)
        assert all(profile.name.endswith("_uncalibrated") for profile in setup.candidates)
        assert setup.alpha == fx.TEST_ONLY_ALPHA


def test_the_single_instrument_setup_is_well_formed() -> None:
    setup = ev.single_instrument_evidence()
    assert setup.sealed_oos_g5 is True
    assert setup.base == fx.G5_BASE_SPEC
    assert setup.planted == (fx.STRONG, fx.WEAK)
    assert setup.noise_seeds == tuple(range(10_000, 10_000 + ev.SINGLE_SEEDS))
    assert setup.planted_seeds == tuple(range(20_000, 20_000 + ev.SINGLE_SEEDS))
    assert setup.run_count() == 3 * ev.SINGLE_SEEDS


def test_the_multi_instrument_setup_is_well_formed() -> None:
    setup = ev.multi_instrument_evidence()
    assert setup.symbols == fx.MULTI_SYMBOLS and len(setup.symbols) == 2
    assert setup.base == fx.BASE_SPEC
    arms = {arm.name: arm for arm in setup.arms}
    assert [arm.kind for arm in setup.arms] == ["all_noise", "all_planted", "mixed"]
    assert arms["all_noise"].effects == (None, None)
    assert arms["all_planted"].effects == (fx.STRONG, fx.STRONG)
    assert arms["mixed"].effects == (fx.STRONG, None)
    seeds = [seed for arm in setup.arms for seed in arm.seeds]
    assert len(seeds) == len(set(seeds)) == 3 * ev.MULTI_SEEDS
    assert setup.run_count() == 3 * ev.MULTI_SEEDS


def test_the_seed_ranges_are_disjoint_from_each_other_and_the_smoke_tests() -> None:
    single = ev.single_instrument_evidence()
    multi = ev.multi_instrument_evidence()
    evidence = [
        *single.noise_seeds,
        *single.planted_seeds,
        *(seed for arm in multi.arms for seed in arm.seeds),
    ]
    assert len(evidence) == len(set(evidence))
    assert min(evidence) >= 10_000  # the smoke tests use 0.., 100.., 200..


def test_the_committed_reports_are_exactly_the_listed_ones() -> None:
    assert sorted(path.stem for path in CALIBRATION_DIR.glob("*.json")) == sorted(REPORT_HASHES)


@pytest.mark.parametrize("name", sorted(REPORT_HASHES))
def test_a_committed_report_matches_its_content_and_its_setup(
    name: str, recorded: dict[str, Any]
) -> None:
    payload = json.loads((CALIBRATION_DIR / f"{name}.json").read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "report_hash"}
    assert payload["report_hash"] == content_hash(body) == REPORT_HASHES[name]
    assert payload["kind"] == "gate_calibration"
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["inputs"] == recorded[name]  # produced by the recorded-version code
    assert [item["profile"] for item in payload["candidates"]] == [
        str(fx.LAX_TEST_ONLY_PROFILE.ref),
        str(fx.STRICT_TEST_ONLY_PROFILE.ref),
    ]
