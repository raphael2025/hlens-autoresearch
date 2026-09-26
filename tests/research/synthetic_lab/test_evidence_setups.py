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
built by the current code must still be deterministic and reproduce the recorded payload
**exactly** once taken back to 2.1.0: every envelope written back as 2.1.0 and every derived hash
(``base_spec_hash``, ``detector.strategy_hash``, each ``effect_hash``, each ``profile_hash``)
replaced by the content hash of the 2.1.0 twin of the very object it hashes. No field is dropped
from the comparison, and every ``*_hash`` of the payload must be one of those derived hashes (a
hash of an object the payload shows only by ref -- the strategy spec, the Profiles -- is therefore
still checked against that object's content).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, Kind, Ref, content_hash
from research.synthetic_lab.gate_calibration import (
    DISCLAIMER,
    GateCalibrationSetup,
    MultiInstrumentCalibrationSetup,
)
from tests.contract_version_support import at_version, envelopes_at
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


JsonPath = tuple[str | int, ...]


def _derived(setup: AnySetup) -> dict[JsonPath, Contract]:
    """Every hash the payload derives from an envelope-carrying object: path -> that object."""
    derived: dict[JsonPath, Contract] = {
        ("base_spec_hash",): setup.base,
        # the detector's candidate is the gate fixtures' candidate (its ref is checked below)
        ("detector", "strategy_hash"): fx.candidate().spec,
    }
    for index, profile in enumerate(setup.candidates):
        derived[("candidate_profiles", index, "profile_hash")] = profile
    if isinstance(setup, GateCalibrationSetup):
        for index, effect in enumerate(setup.planted):
            derived[("planted_effects", index, "effect_hash")] = effect
    else:
        for a, arm in enumerate(setup.arms):
            for j, maybe in enumerate(arm.effects):
                if maybe is not None:
                    path = ("multi_instrument", "arms", a, "instruments", j, "effect_hash")
                    derived[path] = maybe
    return derived


def _hash_paths(value: object, path: JsonPath = ()) -> set[JsonPath]:
    """The path of every non-null ``*_hash`` value in a JSON-like payload."""
    found: set[JsonPath] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_hash") and item is not None:
                found.add((*path, key))
            found |= _hash_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found |= _hash_paths(item, (*path, index))
    return found


def _at(payload: Any, path: JsonPath) -> Any:
    for step in path:
        payload = payload[step]
    return payload


def _as_recorded(setup: AnySetup) -> Any:
    """The current setup's payload taken back to ``RECORDED_VERSION`` (see module docs)."""
    current = json.loads(json.dumps(setup.inputs_payload()))
    derived = _derived(setup)
    assert set(derived) == _hash_paths(current)  # no hash escapes the check
    for path, obj in derived.items():
        assert _at(current, path) == obj.content_hash()  # the current value hashes that object
    twin = envelopes_at(current, RECORDED_VERSION)
    for path, obj in derived.items():
        _at(twin, path[:-1])[path[-1]] = at_version(obj, RECORDED_VERSION).content_hash()
    return twin


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_the_current_setups_are_the_recorded_ones_taken_back_to_2_1_0(
    name: str, recorded: dict[str, Any]
) -> None:
    setup = FACTORIES[name]()
    current = json.loads(json.dumps(setup.inputs_payload()))
    assert current != recorded[name]  # the current envelope is part of the payload
    assert set(_envelopes(current)) == {CONTRACT_SCHEMA_VERSION}
    assert current["detector"]["strategy"] == str(fx.candidate().spec.ref)
    assert _as_recorded(setup) == recorded[name]  # exact: nothing dropped


def test_the_strategy_hash_is_compared_not_dropped(recorded: dict[str, Any]) -> None:
    """``detector.strategy_hash`` is the strategy spec's content hash -- envelope included, so it
    differs between 2.1.0 and 2.2.0 -- and the comparison binds it to that spec's content."""
    name = "single_instrument_evidence"
    spec = fx.candidate().spec
    recorded_hash = recorded[name]["detector"]["strategy_hash"]
    assert recorded_hash == at_version(spec, RECORDED_VERSION).content_hash()
    assert recorded_hash != spec.content_hash()  # envelope-derived: 2.1.0 != 2.2.0
    # a semantic change to the spec (its params) is visible although its ref is unchanged
    changed = spec.model_copy(update={"params": {**dict(spec.params), "lookback": 241}})
    assert changed.ref == spec.ref
    assert at_version(changed, RECORDED_VERSION).content_hash() != recorded_hash
    # and a tampered recorded strategy hash no longer matches the current setup
    tampered = json.loads(json.dumps(recorded[name]))
    tampered["detector"]["strategy_hash"] = "0" * 64
    assert _as_recorded(FACTORIES[name]()) != tampered


def test_a_profile_hash_binds_the_profile_content(recorded: dict[str, Any]) -> None:
    """A Profile appears only by ref and hash: a content change under the same ref is detected."""
    profile = fx.LAX_TEST_ONLY_PROFILE
    [entry] = [
        item
        for item in recorded["single_instrument_evidence"]["candidate_profiles"]
        if item["profile"] == str(profile.ref)
    ]
    assert entry["profile_hash"] == at_version(profile, RECORDED_VERSION).content_hash()
    extra = Ref(kind=Kind.PROFILE, name="test_only_lineage_probe", version="1.0.0")
    changed = profile.model_copy(update={"lineage": (*profile.lineage, extra)})
    assert changed.ref == profile.ref
    assert at_version(changed, RECORDED_VERSION).content_hash() != entry["profile_hash"]


def test_every_hash_of_a_payload_is_a_derived_one() -> None:
    for factory in FACTORIES.values():
        setup = factory()
        paths = _hash_paths(json.loads(json.dumps(setup.inputs_payload())))
        assert paths == set(_derived(setup))
        assert ("detector", "strategy_hash") in paths


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
