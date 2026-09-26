"""Versioned-replay helpers and published Phase 1 identities (ADR-0052 versioned replay V1 ~ V4).

- ``recorded_version``: one published version per write group, otherwise fail closed;
- ``new_group_version``: the current version, never inside a replay scope (a leak fails closed);
- V3: every Phase 1 binding / source binding / registered spec keeps the envelope it was
  published under **and its content hash** (pinned below, computed at 2.0.0 before any bump):
  persisted PIT specs, manifests and checkpoints bind these hashes;
- V4: a spec's binding carries the spec's own envelope.
"""

from __future__ import annotations

import pytest

from core.domain import base
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    contract_schema_version_scope,
)
from infrastructure import contract_version
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE
from infrastructure.collector.binance_exchange_info import EXCHANGE_INFO_SOURCE
from infrastructure.collector.binance_rest import REST_SOURCE
from infrastructure.contract_version import (
    PHASE1_PUBLICATION_VERSION,
    ContractVersionScopeLeak,
    new_group_version,
    recorded_version,
)
from infrastructure.dataset.builder import KNOWN_BINDINGS
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, REGISTERED_UNIVERSES

#: Content hashes of the published Phase 1 identities at 2.0.0 (never regenerated).
PUBLISHED: dict[str, str] = {
    "binance.spot.archive-revision@1.0.0": (
        "8f8becdb16b4b545edab199fa8204d4950ff83f829cbdde83128b42e2ea7111f"
    ),
    "binance.spot.archive.parser@1.0.0": (
        "2906bf803518be37de4f9253f5204e3f934e89654a3f651fa73d6c64497512a7"
    ),
    "binance.spot.delivery-channel@1.0.0": (
        "a13321f75757dab1e27eb01fb32d4b958a675ebcf3d8992b6a906d7e1f5e1654"
    ),
    "binance.spot.exchange-info-publication@1.0.0": (
        "267484c33ddefb2605a00c8da2955f604a38062a96fc754c2e50baf6b4fe6f6f"
    ),
    "binance.spot.exchange-info.decoder@1.0.0": (
        "4ac29f1f0f678e95373973fd76a2ce9e88f211d41d496d4adec4bb6c9c81af1c"
    ),
    "binance.spot.listing-observation@1.0.0": (
        "3fc8fde46ca5ea6a7eb89ee33f0e3ad466e8ad486c6d2c3a174af033364497c9"
    ),
    "binance.spot.listing-status@1.0.0": (
        "c88802a107f478575a84eb1ecd2afe78cbdff222a257ed3760cef048e1d942cc"
    ),
    "binance.spot.publication@1.0.0": (
        "d0e9ae8a0d827e54828f930dc0f0e91ac76535b296deac95379ac313982bb483"
    ),
    "binance.spot.rest-publication@1.0.0": (
        "46b8bdde39e9bad202d183e2add660569d00295bb2bc193aac9053d32856d570"
    ),
    "binance.spot.rest-revision@1.0.0": (
        "2d7fbde4671c9f5296b07ff867c3afd5a859d598afc20b1dff5de660e648be32"
    ),
    "binance.spot.rest.decoder@1.0.0": (
        "2812a2473c178fc495b6a746018f5d7159bdc4e48f23967deaa30de96b20019b"
    ),
    "hlens.availability.archive-event-time-assumption@1.0.0": (
        "8ba5edc9eec91c9206523b9ee56e426dd52ea8319703b9438645c992bfe8252d"
    ),
    "hlens.canonical.availability@1.0.0": (
        "128c437cb5d7c789d593285aa9928f7b92e88458d1bcf150e4fc8cde75ee305c"
    ),
    "hlens.canonical.binance-spot.normalizer@1.0.0": (
        "95433d33c1ffffe506066c9e530f71d10e835e61a1bf233791521827ec6778a1"
    ),
    "hlens.canonical.precedence-map@1.0.0": (
        "2b2f747c24f03362be32c1789360a1cca7ba9d9638796753cd6781b57dd8c91e"
    ),
    "hlens.pit.maximal-head@1.0.0": (
        "c3681bdca7487b3a9b8cdccd3c713681d698f31684ed8d7b3b94adacddebc7bc"
    ),
    "source:binance.public.spot.archive@1.0.0": (
        "f384d3ed4015a9fb34ae1762a851e3f1ea2660c2a64ab22306422bdcbca0cca8"
    ),
    "source:binance.public.spot.exchange-info@1.0.0": (
        "3d189190e06ce6fbe7026a0512a31582c1e1a4323a9174c99fa17ab212f7558d"
    ),
    "source:binance.public.spot.rest@1.0.0": (
        "95656363da0610b6bd039b3351d4a0768720b1fb81a3b425b1b14e81207892b5"
    ),
    "universe-binding:binance.spot.btc-eth": (
        "e1112891b2be4974055a150c598e625f31ecd3f064d4f93d4dba5f01e1fa4297"
    ),
    "universe:binance.spot.btc-eth@1.0.0": (
        "2d2bbae12a8da191d1cb73fd71d2b9a04d1b72aa10016f8fe37e39c4eabc8568"
    ),
}


def _published_objects() -> dict[str, base.Contract]:
    items: dict[str, base.Contract] = {
        f"{binding.policy_id}@{binding.version}": binding
        for binding in (*KNOWN_BINDINGS, PIT_BINDING)
    }
    for source in (ARCHIVE_SOURCE, REST_SOURCE, EXCHANGE_INFO_SOURCE):
        items[f"source:{source.source_id}@{source.version}"] = source
    for (name, version), spec in REGISTERED_UNIVERSES.items():
        items[f"universe:{name}@{version}"] = spec
    items[f"universe-binding:{FIRST_SLICE_UNIVERSE.name}"] = FIRST_SLICE_UNIVERSE.binding()
    return items


# ------------------------------------------------------------------ recorded_version


def test_a_group_records_one_published_version() -> None:
    for version in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        assert recorded_version([version, version], what="unit u") == version


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ([], "records 0 contract versions"),
        (["2.0.0", "2.99.0"], "records 2 contract versions"),
        (["2.99.0"], "not one of the published"),
        (["3.0.0"], "not one of the published"),
        ([None], "not one of the published"),
    ],
)
def test_no_mixed_or_unpublished_version_is_replayed(values: list[object], match: str) -> None:
    with pytest.raises(CatalogIntegrityError, match=match):
        recorded_version(values, what="unit u")


def test_a_new_group_is_written_at_the_current_version_never_inside_a_replay_scope() -> None:
    assert new_group_version() == CONTRACT_SCHEMA_VERSION
    with contract_schema_version_scope(PUBLISHED_CONTRACT_SCHEMA_VERSIONS[0]):  # noqa: SIM117
        with pytest.raises(ContractVersionScopeLeak, match="replay scope"):
            contract_version.new_group_version()
    assert new_group_version() == CONTRACT_SCHEMA_VERSION


# ------------------------------------------------------------------ V3 / V4


def test_published_identities_keep_their_envelope_and_hash() -> None:
    assert PHASE1_PUBLICATION_VERSION == "2.0.0"
    found = _published_objects()
    assert set(found) == set(PUBLISHED)
    for name, item in found.items():
        assert item.schema_version == PHASE1_PUBLICATION_VERSION, name
        assert item.content_hash() == PUBLISHED[name], name


def test_every_known_binding_is_published() -> None:
    """KNOWN_BINDINGS is the registry a spec's bindings are checked against: all pinned."""
    assert len(KNOWN_BINDINGS) == 15
    assert {binding.schema_version for binding in KNOWN_BINDINGS} == {PHASE1_PUBLICATION_VERSION}


def test_a_binding_carries_its_specs_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = FIRST_SLICE_UNIVERSE.binding()
    assert binding.schema_version == FIRST_SLICE_UNIVERSE.schema_version
    assert binding.spec_hash == FIRST_SLICE_UNIVERSE.content_hash()
    later = "2.99.0"
    monkeypatch.setattr(
        base, "PUBLISHED_CONTRACT_SCHEMA_VERSIONS", (*PUBLISHED_CONTRACT_SCHEMA_VERSIONS, later)
    )
    with contract_schema_version_scope(later):
        spec = FIRST_SLICE_UNIVERSE.model_validate(
            FIRST_SLICE_UNIVERSE.model_dump(mode="json", exclude={"schema_version"})
        )
    assert spec.schema_version == later
    assert spec.binding().schema_version == later  # built outside the scope: inherited
    assert spec.binding() != binding  # another envelope is another spec (ADR-0008)
