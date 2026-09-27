"""The replay-at-recorded-version scope (ADR-0052 "Implementation note — versioned replay").

Codex M0 review conditions: the scope only takes effect for a *defaulted* ``schema_version``
(explicit envelopes and already-built nested objects are never rewritten), it is restored on exit
(context manager, also on error; per thread), it refuses an unpublished version, and without a
scope construction is bit-identical to what it was before the mechanism existed.

A published *later* minor is simulated where needed by extending
``PUBLISHED_CONTRACT_SCHEMA_VERSIONS`` for one test (``_later``), so the mechanism is exercised
at any current version.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain import base
from core.domain.base import (
    CONTRACT_SCHEMA_MAJOR,
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Kind,
    Ref,
    contract_schema_version_scope,
    parse_semver,
    scoped_contract_schema_version,
)
from core.domain.research import ValidationReport

#: A later minor that no code has published (used only through ``_later``).
LATER = "2.99.0"
GOLDEN = Path(__file__).resolve().parent / "golden" / "v2_0_0"


@pytest.fixture
def _later(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(
        base, "PUBLISHED_CONTRACT_SCHEMA_VERSIONS", (*PUBLISHED_CONTRACT_SCHEMA_VERSIONS, LATER)
    )
    yield LATER


def _ref() -> Ref:
    return Ref(kind=Kind.PROFILE, name="p", version="1.0.0")


def test_published_versions_ascend_within_the_major_and_end_at_the_current_one() -> None:
    parsed = [parse_semver(version) for version in PUBLISHED_CONTRACT_SCHEMA_VERSIONS]
    assert all(int(match.group("major")) == CONTRACT_SCHEMA_MAJOR for match in parsed)
    keys = [(int(m.group("minor")), int(m.group("patch"))) for m in parsed]
    assert keys == sorted(set(keys))
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[0] == "2.0.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[-1] == CONTRACT_SCHEMA_VERSION


def test_without_a_scope_construction_and_schemas_are_unchanged() -> None:
    assert scoped_contract_schema_version() is None
    assert _ref().schema_version == CONTRACT_SCHEMA_VERSION
    assert Ref.model_fields["schema_version"].default == CONTRACT_SCHEMA_VERSION
    schema = Ref.model_json_schema()["properties"]["schema_version"]
    assert schema["default"] == CONTRACT_SCHEMA_VERSION


def test_only_defaulted_envelopes_take_the_scope_version(_later: str) -> None:
    outside = _ref()  # built before the scope: an explicit nested object keeps its version
    with contract_schema_version_scope(_later) as version:
        assert version == _later and scoped_contract_schema_version() == _later
        assert _ref().schema_version == _later
        assert Ref.model_validate_json('{"kind":"profile","name":"p","version":"1.0.0"}') == (
            Ref(schema_version=_later, kind=Kind.PROFILE, name="p", version="1.0.0")
        )
        # explicit envelopes are never rewritten, on either entry point
        explicit = Ref(schema_version="2.0.0", kind=Kind.PROFILE, name="p", version="1.0.0")
        assert explicit.schema_version == "2.0.0"
        assert Ref.model_validate_json(outside.model_dump_json()).schema_version == (
            outside.schema_version
        )
        # nested: a defaulted nested dict takes the scope, an explicit one keeps its version
        payload = json.loads((GOLDEN / "validation_report.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        first, *others = payload["gates"]
        defaulted = {key: value for key, value in first.items() if key != "schema_version"}
        report = ValidationReport.model_validate(
            {
                **{key: value for key, value in payload.items() if key != "schema_version"},
                "gates": [defaulted, *others],
            }
        )
        assert report.schema_version == _later
        assert [gate.schema_version for gate in report.gates] == [_later, "2.0.0", "2.0.0"]
        assert report.subject.schema_version == "2.0.0"
        binding = PolicyBinding(
            role=PolicyRole.PARSER, policy_id="p", version="1.0.0", policy_hash="0" * 64
        )
        assert binding.schema_version == _later
    assert outside.schema_version == CONTRACT_SCHEMA_VERSION
    assert scoped_contract_schema_version() is None
    assert _ref().schema_version == CONTRACT_SCHEMA_VERSION


@pytest.mark.parametrize("version", ["2.99.0", "3.0.0", "1.0.0", "2.0", "x", 2])
def test_an_unpublished_version_is_refused(version: object) -> None:
    with pytest.raises(ValueError, match="已发布"):  # noqa: SIM117
        with contract_schema_version_scope(version):  # type: ignore[arg-type]
            pass  # pragma: no cover
    assert scoped_contract_schema_version() is None


def test_the_scope_is_restored_after_an_error_and_nests(_later: str) -> None:
    first = PUBLISHED_CONTRACT_SCHEMA_VERSIONS[0]
    with pytest.raises(RuntimeError, match="boom"):  # noqa: SIM117
        with contract_schema_version_scope(_later):
            raise RuntimeError("boom")
    assert scoped_contract_schema_version() is None
    with contract_schema_version_scope(_later):
        with contract_schema_version_scope(first):
            assert _ref().schema_version == first
        assert _ref().schema_version == _later
    assert scoped_contract_schema_version() is None


def test_the_scope_does_not_cross_threads(_later: str) -> None:
    seen: list[object] = []

    def other() -> None:
        seen.append((scoped_contract_schema_version(), _ref().schema_version))

    with contract_schema_version_scope(_later):
        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
    assert seen == [(None, CONTRACT_SCHEMA_VERSION)]
