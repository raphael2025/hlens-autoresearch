"""Verified pairing of the feature and price manifests of one research chain (backlog E1).

One research chain needs **two** Research Dataset manifests over the same market data: F4
(``infrastructure.feature.dataset``) evaluates features at every bar and needs an **interval**
simulation spec (a PIT view at each evaluation time), while the P4 / P5 bar path
(``infrastructure.bars.dataset``) accepts only a **point** simulation spec (one PIT view per bar).
Nothing bound the two together. ``pair_manifests`` does: the caller gives a ``DatasetBuilder`` and
the two manifest content hashes, never manifest objects, and gets a ``ManifestPair`` only when both
manifests load and verify and describe the same market data.

1. **verified load** — each manifest is loaded through ``builder``'s own verifying
   ``ManifestStore`` (``load_manifest``: row JSON re-hashed, contract re-validated, the whole build
   re-derived). An unknown hash, a forged row or a non-builder verifier is refused
   (``DatasetBindingError`` / ``CatalogIntegrityError``);
2. **shape** — the feature manifest is an interval simulation ``[start, end)``, the price manifest
   a point simulation;
3. **time relation** — the price view is the end of the feature interval:
   ``price.simulation_time == feature.simulation_end``. The half-open feature interval evaluates
   strictly before ``end``; the price view at ``end`` is the first instant after it, so the prices
   (and ``price_cutoff``, which defaults to and may not exceed ``simulation_time``) know no more
   than the feature chain could have known at the end of its interval;
4. **knowledge** — equal ``knowledge_cutoff`` (one knowledge state; a different cutoff could select
   another revision of the same bar);
5. **upstream snapshots** — equal ``snapshot_bindings`` (the same Canonical / Raw / listing /
   quality snapshots);
6. **policies** — the same ADR-0032 choice (``assumption_bound``: both bind the archive event-time
   assumption, or neither), then equal availability, precedence, parser and PIT-rule bindings;
7. **dataset** — the same universe spec binding, the same Research Dataset table and the same data
   window (``time_range_start`` / ``time_range_end``);
8. **instruments** — the price view's member episodes are exactly the episodes that are members
   for the **whole** feature interval (their spans cover ``[start, end)`` without gaps), each with
   the same listing revision at ``end`` as the price view; any partial member of the feature
   interval is refused (the instrument set changed within the chain). The excluded episodes must
   match too (an exclusion spanning part of the interval counts as excluded);
9. **lineage** — the same set of Canonical tables, and every revision the price view uses (its
   ``lineage``) is one the feature view saw; the price view's evidence gaps are a subset of the
   feature view's and the quality report ids are equal.

The pair hash binds the two manifest hashes under the hash of the rule that paired them
(``PAIR_RULE_HASH`` v2, ``PAIR_RULE_V3_HASH`` v3), so a change of rule is a change of pair.

**Limits** (not provable from ``ResearchDatasetManifest`` fields alone):

- the data type is not a manifest field: the lineage relation (same Canonical tables, price
  revisions seen by the feature view) stands in for it, and the bar path itself refuses a manifest
  without ``klines_1m`` rows;
- feature-only revisions (in the feature lineage, not the price lineage) are accepted as revisions
  superseded within the interval; that they were superseded, rather than merely absent at ``end``,
  is not re-proven here (the per-bar re-selection of each path proves its own rows);
- a revision that becomes available exactly at ``end``, or a listing change within the interval,
  makes the pair fail closed (refused), even where a finer rule might accept it;
- a ``ManifestPair`` is a record, not a proof: a consumer that must know the pair holds calls
  ``pair_manifests`` again with the two hashes.

``manifest_cache`` (default ``None``: both loads re-verify) is an explicit ``VerifiedManifestCache``
for step 1 (``infrastructure.bars.verified``: a proof is reused only by the same builder while
every snapshot it read is unchanged); steps 2 - 9 always run.

**v3 evidence manifests (ADR-0077; C1-CONSUMERS).** ``evidence_verifier`` (default ``None``: the
v2 path above, unchanged) lets step 1 load either form (``load_verified_any``). Two v2 manifests
are paired exactly as above, under ``PAIR_RULE_HASH``, unchanged. Two v3 manifests are paired by
their **own rule** (``PAIR_RULE_V3_HASH``: step 1 loads through ``load_any`` / ``load_verified_any``
and the ``StreamingEvidenceVerifier``'s own re-derivation, not ``load_manifest``, so it is a
distinct rule text and hash from ``PAIR_RULE_HASH``, same pair hash formula (``pair_hash_of``) over
the two v3 content hashes, which bind the v3 dataset rule), steps 2 - 7 on the manifest fields
(plus: equal recorded ``data_type``), and steps 8 - 9 by ordered merges of the evidence streams
instead of whole-manifest sets:

- **instruments** — the feature ``members`` stream is grouped by episode (it is ordered by
  ``(episode key, effective_from)``, ADR-0077 §2): one episode's spans at a time decide whole /
  partial as above, and the whole episodes are merged against the price ``members`` stream (one
  entry per episode, same order); ``exclusions`` streams are merged as distinct episode keys;
- **lineage** — the listing prefixes of both ``lineage`` streams (by revision id) are merged
  (price subset of feature, record for record), as are the listing prefixes of the
  ``evidence_gaps`` streams; the data lineage and data gaps are read with each dataset's rows
  (``iter_dataset_chunks``) as key groups ordered by ``(venue symbol, UTC day of the event,
  observation key)`` (the builder's ``(symbol, slice, key)`` row order for 1-minute bars, whose
  slice is the UTC day), and every price key group must be a feature key group holding each of its
  revisions with the same lineage and the same gap. A key group out of that order in either
  dataset is refused (the pair cannot be proven by the merge). The Canonical table sets and the
  ``quality_reports`` streams (report id and partition, record for record) must be equal.

Held: one episode's spans, one key group per dataset, one chunk per dataset and one lookahead per
stream. Mixing a v2 and a v3 manifest in one pair is refused. The whole-manifest checks of the
limits above (feature-only revisions, a listing change within the interval) are unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetQualityReportRef,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
)
from core.domain.base import Contract, canonical_json, content_hash
from infrastructure.bars.dataset import DatasetBarsError
from infrastructure.bars.verified import (
    VerifiedManifestCache,
    load_verified_any,
    load_verified_manifest,
)
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import CANONICAL_INSTRUMENT_LISTINGS
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.dataset.evidence import evidence_record_bytes
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.feature.dataset import (
    DatasetRowEvidence,
    evidence_catalog,
    iter_dataset_chunks,
    iter_manifest_evidence,
)
from infrastructure.pit.assumption import assumption_bound

__all__ = [
    "PAIR_RULE",
    "PAIR_RULE_HASH",
    "PAIR_RULE_V3",
    "PAIR_RULE_V3_HASH",
    "ManifestPair",
    "ManifestPairError",
    "pair_hash_of",
    "pair_manifests",
]

PAIR_RULE_ID: Final = "hlens.dataset.manifest-pair"
PAIR_RULE_VERSION: Final = "1.0.0"
PAIR_RULE: Final[dict[str, Any]] = {
    "rule": PAIR_RULE_ID,
    "version": PAIR_RULE_VERSION,
    "adr": "ADR-0037 implementation note (E1 manifest pairing, 2026-09-25)",
    "load": "both manifests through the builder's verifying ManifestStore (load_manifest)",
    "shape": "feature = interval simulation [start, end); price = point simulation",
    "time": "price.simulation_time == feature.simulation_end",
    "knowledge": "equal knowledge_cutoff",
    "snapshots": "equal snapshot_bindings",
    "policies": "same ADR-0032 assumption choice; equal availability, precedence, parser and "
    "point-in-time bindings",
    "dataset": "equal universe spec binding, dataset table and data window",
    "instruments": "price members == episodes that are members for the whole feature interval, "
    "same listing revision at end; no partial member; equal excluded episodes",
    "lineage": "equal Canonical table sets; price lineage subset of feature lineage; price "
    "evidence gaps subset of feature gaps; equal quality report ids",
}
PAIR_RULE_HASH: Final = hashlib.sha256(canonical_json(PAIR_RULE).encode("utf-8")).hexdigest()

#: v3 (ADR-0077; C1-CONSUMERS): a distinct rule from ``PAIR_RULE`` — step 1 loads through
#: ``load_any`` / ``load_verified_any`` and the ``StreamingEvidenceVerifier``'s own re-derivation
#: (never ``load_manifest``), and steps 8 - 9 are ordered merges of the evidence streams (module
#: docs) instead of whole-manifest sets. v2 pairing (``PAIR_RULE`` / ``PAIR_RULE_HASH``) is
#: unchanged by this: two v2 manifests are never bound under ``PAIR_RULE_V3_HASH``.
PAIR_RULE_V3_ID: Final = "hlens.dataset.manifest-pair-v3"
PAIR_RULE_V3_VERSION: Final = "1.0.0"
PAIR_RULE_V3: Final[dict[str, Any]] = {
    "rule": PAIR_RULE_V3_ID,
    "version": PAIR_RULE_V3_VERSION,
    "adr": "ADR-0077 implementation note (C1-CONSUMERS v3 manifest pairing, 2026-09-28)",
    "load": "both manifests through load_any (load_verified_any) over the builder's own catalog, "
    "proven by the StreamingEvidenceVerifier's own re-derivation (verify_evidence_manifest)",
    "shape": "feature = interval simulation [start, end); price = point simulation",
    "time": "price.simulation_time == feature.simulation_end",
    "knowledge": "equal knowledge_cutoff",
    "snapshots": "equal snapshot_bindings",
    "policies": "same ADR-0032 assumption choice; equal availability, precedence, parser and "
    "point-in-time bindings",
    "dataset": "equal universe spec binding, dataset table and data window; equal recorded "
    "data_type",
    "instruments": "the feature members stream grouped by episode (ordered by (episode key, "
    "effective_from)): whole / partial decided per episode, the whole episodes merged against the "
    "price members stream (one entry per episode, same order), same listing revision at end; "
    "exclusions streams merged as distinct episode keys",
    "lineage": "listing prefixes of both lineage streams (by revision id) merged, price subset of "
    "feature; listing prefixes of both evidence_gaps streams merged the same way; data lineage and "
    "data gaps merged by dataset row key group (venue symbol, UTC day, observation key), every "
    "price key group a feature key group with the same lineage and gap; equal Canonical table "
    "sets; equal quality_reports streams (report id and partition)",
}
PAIR_RULE_V3_HASH: Final = hashlib.sha256(canonical_json(PAIR_RULE_V3).encode("utf-8")).hexdigest()


class ManifestPairError(DatasetBarsError):
    """The two manifests do not describe the same market data of one chain (fail closed)."""


@dataclass(frozen=True, slots=True)
class ManifestPair:
    """The verified pairing of one chain's feature (interval) and price (point) manifests.

    ``pair_hash`` binds under either pairing rule's hash (``PAIR_RULE_HASH`` v2,
    ``PAIR_RULE_V3_HASH`` v3): the record itself does not carry which rule paired it, so both are
    accepted here (``pair_manifests`` is what proves the pairing; this only re-checks the binding).
    """

    feature_manifest_hash: str
    price_manifest_hash: str
    pair_hash: str

    def __post_init__(self) -> None:
        bound = (
            pair_hash_of(self.feature_manifest_hash, self.price_manifest_hash),
            pair_hash_of(
                self.feature_manifest_hash, self.price_manifest_hash, rule_hash=PAIR_RULE_V3_HASH
            ),
        )
        if self.pair_hash not in bound:
            raise ManifestPairError(
                "pair_hash does not bind these two manifest hashes under either pairing rule"
            )


def pair_manifests(
    builder: DatasetBuilder,
    feature_manifest_hash: str,
    price_manifest_hash: str,
    *,
    manifest_cache: VerifiedManifestCache | None = None,
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> ManifestPair:
    """Load and verify both manifests, prove they describe the same market data, or refuse.

    ``evidence_verifier``: module docs (v3); ``None`` is the v2 path, unchanged."""
    if evidence_verifier is None:
        feature = load_verified_manifest(builder, feature_manifest_hash, manifest_cache)
        price = load_verified_manifest(builder, price_manifest_hash, manifest_cache)
        _require_pair(feature, price)
    else:
        f_any = load_verified_any(builder, feature_manifest_hash, manifest_cache, evidence_verifier)
        p_any = load_verified_any(builder, price_manifest_hash, manifest_cache, evidence_verifier)
        if isinstance(f_any, ResearchDatasetManifest) and isinstance(
            p_any, ResearchDatasetManifest
        ):
            _require_pair(f_any, p_any)
        elif isinstance(f_any, ResearchDatasetEvidenceManifest) and isinstance(
            p_any, ResearchDatasetEvidenceManifest
        ):
            _require_evidence_pair(evidence_verifier, f_any, p_any)
        else:
            raise _refuse("a v2 and a v3 (evidence) manifest are never one chain")
        return ManifestPair(
            feature_manifest_hash=f_any.content_hash(),
            price_manifest_hash=p_any.content_hash(),
            pair_hash=pair_hash_of(
                f_any.content_hash(), p_any.content_hash(), rule_hash=PAIR_RULE_V3_HASH
            ),
        )
    return ManifestPair(
        feature_manifest_hash=feature.content_hash(),
        price_manifest_hash=price.content_hash(),
        pair_hash=pair_hash_of(feature.content_hash(), price.content_hash()),
    )


def pair_hash_of(
    feature_manifest_hash: str, price_manifest_hash: str, *, rule_hash: str = PAIR_RULE_HASH
) -> str:
    """The pair hash binding these two manifest hashes under ``rule_hash``.

    ``rule_hash`` defaults to ``PAIR_RULE_HASH`` (v2, unchanged); pass ``PAIR_RULE_V3_HASH`` for a
    v3 (evidence) pair. Public so a consumer holding only a ``ManifestPair`` record (no builder,
    e.g. the research validator) can re-check that its ``pair_hash`` binds its two manifest hashes;
    that re-check is not a re-proof of the pairing itself (only ``pair_manifests`` proves it).
    """
    return content_hash(
        {
            "rule": rule_hash,
            "feature_manifest": feature_manifest_hash,
            "price_manifest": price_manifest_hash,
        }
    )


# ---------------------------------------------------------------------------------------------


def _refuse(message: str) -> ManifestPairError:
    return ManifestPairError(f"manifest pair refused: {message}")


def _require_pair(feature: ResearchDatasetManifest, price: ResearchDatasetManifest) -> None:
    start, end = _require_upstream(feature, price)  # steps 2-7
    _require_instruments(feature, price, start, end)  # step 8
    _require_lineage(feature, price)  # step 9


def _require_upstream(
    feature: ResearchDatasetManifest | ResearchDatasetEvidenceManifest,
    price: ResearchDatasetManifest | ResearchDatasetEvidenceManifest,
) -> tuple[datetime, datetime]:
    """Steps 2-7 (manifest fields only, either form); the feature interval."""
    f_spec, p_spec = feature.point_in_time, price.point_in_time
    start, end = _require_times(f_spec, p_spec)  # steps 2-4
    if f_spec.snapshot_bindings != p_spec.snapshot_bindings:  # step 5
        tables = sorted(
            table
            for table in {*f_spec.snapshot_bindings, *p_spec.snapshot_bindings}
            if f_spec.snapshot_bindings.get(table) != p_spec.snapshot_bindings.get(table)
        )
        raise _refuse(f"the upstream snapshots differ for {tables}")
    _require_policies(f_spec, p_spec)  # step 6
    if feature.universe_spec != price.universe_spec:  # step 7
        raise _refuse(
            f"universe spec {feature.universe_spec.name}@{feature.universe_spec.version} "
            f"!= {price.universe_spec.name}@{price.universe_spec.version} (or another hash)"
        )
    f_data, p_data = feature.dataset, price.dataset
    if f_data.table != p_data.table:
        raise _refuse(f"dataset table {f_data.table} != {p_data.table}")
    if (f_data.time_range_start, f_data.time_range_end) != (
        p_data.time_range_start,
        p_data.time_range_end,
    ):
        raise _refuse(
            f"data window [{f_data.time_range_start.isoformat()}, "
            f"{f_data.time_range_end.isoformat()}) != [{p_data.time_range_start.isoformat()}, "
            f"{p_data.time_range_end.isoformat()})"
        )
    return start, end


def _require_times(f_spec: PointInTimeSpec, p_spec: PointInTimeSpec) -> tuple[datetime, datetime]:
    start, end = f_spec.simulation_start, f_spec.simulation_end
    if start is None or end is None:
        raise _refuse("the feature manifest must be an interval simulation [start, end)")
    if p_spec.simulation_time is None:
        raise _refuse("the price manifest must be a point simulation")
    if p_spec.simulation_time != end:
        raise _refuse(
            f"the price view {p_spec.simulation_time.isoformat()} is not the end of the feature "
            f"interval {end.isoformat()}"
        )
    if f_spec.knowledge_cutoff != p_spec.knowledge_cutoff:
        raise _refuse(
            f"knowledge cutoff {f_spec.knowledge_cutoff.isoformat()} (feature) != "
            f"{p_spec.knowledge_cutoff.isoformat()} (price)"
        )
    return start, end


def _require_policies(f_spec: PointInTimeSpec, p_spec: PointInTimeSpec) -> None:
    f_assumed, p_assumed = assumption_bound(f_spec), assumption_bound(p_spec)
    if f_assumed != p_assumed:
        raise _refuse(
            "the ADR-0032 archive event-time assumption is bound by the "
            f"{'feature' if f_assumed else 'price'} manifest only"
        )
    for field in (
        "availability_bindings",
        "precedence_bindings",
        "parser_bindings",
        "point_in_time_binding",
    ):
        if getattr(f_spec, field) != getattr(p_spec, field):
            raise _refuse(f"{field} differ")


def _episodes(entries: Iterable[UniverseMember | UniverseExclusion]) -> set[str]:
    return {entry.episode.observation_key() for entry in entries}


def _require_instruments(
    feature: ResearchDatasetManifest,
    price: ResearchDatasetManifest,
    start: datetime,
    end: datetime,
) -> None:
    spans: dict[str, list[UniverseMember]] = {}
    for member in feature.members:
        spans.setdefault(member.episode.observation_key(), []).append(member)
    whole: dict[str, str] = {}  # episode -> its listing revision at the end of the interval
    partial: list[str] = []
    for key, entries in sorted(spans.items()):
        entries.sort(key=lambda entry: entry.effective_from or start)
        reach = start
        for entry in entries:
            if entry.effective_from != reach or entry.effective_until is None:
                break
            reach = entry.effective_until
        if reach == end:
            whole[key] = entries[-1].listing_revision_id
        else:
            partial.append(key)
    if partial:
        raise _refuse(f"episodes {partial} are members for part of the feature interval only")
    priced = {
        member.episode.observation_key(): member.listing_revision_id for member in price.members
    }
    if set(whole) != set(priced):
        raise _refuse(
            f"instrument set differs: feature-only {sorted(set(whole) - set(priced))}, "
            f"price-only {sorted(set(priced) - set(whole))}"
        )
    moved = sorted(key for key, revision in priced.items() if whole[key] != revision)
    if moved:
        raise _refuse(f"episodes {moved} have another listing revision at the end of the interval")
    f_excluded, p_excluded = _episodes(feature.exclusions), _episodes(price.exclusions)
    if f_excluded != p_excluded:
        raise _refuse(
            f"excluded episodes differ: feature-only {sorted(f_excluded - p_excluded)}, "
            f"price-only {sorted(p_excluded - f_excluded)}"
        )


def _require_lineage(feature: ResearchDatasetManifest, price: ResearchDatasetManifest) -> None:
    f_tables = {item.canonical_table for item in feature.lineage}
    p_tables = {item.canonical_table for item in price.lineage}
    if f_tables != p_tables:
        raise _refuse(f"Canonical tables differ: {sorted(f_tables)} != {sorted(p_tables)}")
    unseen = sorted(
        item.canonical_revision_id for item in set(price.lineage) - set(feature.lineage)
    )
    if unseen:
        raise _refuse(
            f"{len(unseen)} price revision(s) were never seen by the feature view "
            f"(first {unseen[0]})"
        )
    gaps = sorted(gap.revision_id for gap in set(price.evidence_gaps) - set(feature.evidence_gaps))
    if gaps:
        raise _refuse(f"evidence gaps of {gaps} are bound by the price manifest only")
    if feature.quality_report_ids != price.quality_report_ids:
        raise _refuse("quality report ids differ")


# =========================================================================================
# v3: steps 8 - 9 by ordered merges over the evidence streams (module docs)
# =========================================================================================

_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_VENUE_SYMBOL: Final = {item.symbol: venue for venue, item in rules.SYMBOLS.items()}
_END: Final = object()
#: ``(venue symbol, UTC day of the event, observation key)`` of one key group of rows.
type _GroupKey = tuple[str, str, str]
#: revision -> (its lineage record bytes, its gap record bytes or None), within one key group.
type _GroupRevisions = dict[str, tuple[bytes, bytes | None]]


class _Ahead:
    """One-item lookahead over an iterator (``_END`` when exhausted)."""

    def __init__(self, items: Iterator[Any]) -> None:
        self._items = items
        self._next: Any = _END

    def peek(self) -> Any:
        if self._next is _END:
            self._next = next(self._items, _END)
        return self._next

    def take(self) -> Any:
        found = self.peek()
        self._next = _END
        return found


def _require_evidence_pair(
    verifier: StreamingEvidenceVerifier,
    feature: ResearchDatasetEvidenceManifest,
    price: ResearchDatasetEvidenceManifest,
) -> None:
    start, end = _require_upstream(feature, price)  # steps 2-7
    if feature.data_type != price.data_type:
        raise _refuse(f"data type {feature.data_type} != {price.data_type}")
    _require_evidence_instruments(verifier, feature, price, start, end)  # step 8
    _require_evidence_lineage(verifier, feature, price)  # step 9


def _episode_groups(
    records: Iterator[Contract], which: str
) -> Iterator[tuple[str, list[UniverseMember | UniverseExclusion]]]:
    """Adjacent entries of one episode (the stream is ordered by episode key, ADR-0077 §2)."""
    key: str | None = None
    entries: list[UniverseMember | UniverseExclusion] = []
    for record in records:
        if not isinstance(record, UniverseMember | UniverseExclusion):  # pragma: no cover
            raise _refuse(f"a {which} record is not a universe entry")
        found = record.episode.observation_key()
        if found != key:
            if key is not None:
                if found < key:
                    raise _refuse(f"the {which} stream is not in episode order")
                yield key, entries
            key, entries = found, []
        entries.append(record)
    if key is not None:
        yield key, entries


def _episode_keys(records: Iterator[Contract], which: str) -> Iterator[str]:
    """The distinct episode keys of an ordered universe stream, in order."""
    for key, _ in _episode_groups(records, which):
        yield key


def _require_evidence_instruments(
    verifier: StreamingEvidenceVerifier,
    feature: ResearchDatasetEvidenceManifest,
    price: ResearchDatasetEvidenceManifest,
    start: datetime,
    end: datetime,
) -> None:
    """Step 8 by merge: whole feature episodes == price episodes, same listing revision at
    ``end``; equal excluded episodes."""
    with ExitStack() as stack:
        f_members = stack.enter_context(
            iter_manifest_evidence(verifier, feature, EvidenceStream.MEMBERS)
        )
        p_members = stack.enter_context(
            iter_manifest_evidence(verifier, price, EvidenceStream.MEMBERS)
        )
        priced = _Ahead(_episode_groups(p_members, "price members"))
        for key, entries in _episode_groups(f_members, "feature members"):
            entries.sort(key=lambda entry: entry.effective_from or start)
            reach = start
            for entry in entries:
                if entry.effective_from != reach or entry.effective_until is None:
                    break
                reach = entry.effective_until
            if reach != end:
                raise _refuse(f"episodes {[key]} are members for part of the feature interval only")
            found = priced.peek()
            if found is not _END and found[0] < key:
                raise _refuse(f"instrument set differs: feature-only [], price-only {[found[0]]}")
            if found is _END or found[0] != key:
                raise _refuse(f"instrument set differs: feature-only {[key]}, price-only []")
            priced.take()
            if found[1][-1].listing_revision_id != entries[-1].listing_revision_id:
                raise _refuse(
                    f"episodes {[key]} have another listing revision at the end of the interval"
                )
        rest = priced.peek()
        if rest is not _END:
            raise _refuse(f"instrument set differs: feature-only [], price-only {[rest[0]]}")

    with ExitStack() as stack:
        f_excluded = _Ahead(
            _episode_keys(
                stack.enter_context(
                    iter_manifest_evidence(verifier, feature, EvidenceStream.EXCLUSIONS)
                ),
                "feature exclusions",
            )
        )
        p_excluded = _Ahead(
            _episode_keys(
                stack.enter_context(
                    iter_manifest_evidence(verifier, price, EvidenceStream.EXCLUSIONS)
                ),
                "price exclusions",
            )
        )
        while True:
            f_key, p_key = f_excluded.peek(), p_excluded.peek()
            if f_key is _END and p_key is _END:
                return
            if f_key == p_key:
                f_excluded.take()
                p_excluded.take()
            elif p_key is _END or (f_key is not _END and f_key < p_key):
                raise _refuse(f"excluded episodes differ: feature-only {[f_key]}, price-only []")
            else:
                raise _refuse(f"excluded episodes differ: feature-only [], price-only {[p_key]}")


def _listing_prefix(records: Iterator[Contract], which: str) -> Iterator[tuple[str, bytes]]:
    """``(revision id, record bytes)`` of the stream's listing prefix (by revision id, ADR-0077
    §2), stopping at its first data record."""
    previous: str | None = None
    for record in records:
        if isinstance(record, SelectedRevisionLineage):
            table, revision = record.canonical_table, record.canonical_revision_id
        elif isinstance(record, AvailabilityEvidenceGap):
            table, revision = record.table, record.revision_id
        else:  # pragma: no cover - the streams' models
            raise _refuse(f"a {which} record is malformed")
        if table != _LISTINGS:
            return
        if previous is not None and revision <= previous:
            raise _refuse(f"the {which} listing prefix is not in revision order")
        previous = revision
        yield revision, evidence_record_bytes(record)


def _merge_listing(
    verifier: StreamingEvidenceVerifier,
    feature: ResearchDatasetEvidenceManifest,
    price: ResearchDatasetEvidenceManifest,
    stream: EvidenceStream,
    refusal: str,
) -> tuple[bool, bool]:
    """Every price listing record of ``stream`` is a feature one, byte for byte; whether each
    side has any."""
    with ExitStack() as stack:
        f_records = _Ahead(
            _listing_prefix(
                stack.enter_context(iter_manifest_evidence(verifier, feature, stream)),
                f"feature {stream.value}",
            )
        )
        p_records = _listing_prefix(
            stack.enter_context(iter_manifest_evidence(verifier, price, stream)),
            f"price {stream.value}",
        )
        f_any = f_records.peek() is not _END
        p_any = False
        for revision, data in p_records:
            p_any = True
            while (found := f_records.peek()) is not _END and found[0] < revision:
                f_records.take()
            if found is _END or found != (revision, data):
                raise _refuse(refusal.format(revision=revision))
            f_records.take()
    return f_any, p_any


def _key_groups(
    chunks: Iterator[tuple[DatasetRowEvidence, ...]], which: str
) -> Iterator[tuple[_GroupKey, _GroupRevisions]]:
    """One dataset's rows as key groups in ``(venue symbol, day, key)`` order (module docs)."""
    name: tuple[str, str] | None = None
    current: _GroupKey | None = None
    revisions: _GroupRevisions = {}
    for chunk in chunks:
        for item in chunk:
            row = item.row
            if (row["symbol"], row["observation_key"]) != name:
                if current is not None:
                    yield current, revisions
                venue = _VENUE_SYMBOL.get(row["symbol"])
                if venue is None:
                    raise _refuse(f"the {which} dataset has rows of an unknown symbol")
                order = (
                    venue,
                    row["event_time"].astimezone(UTC).date().isoformat(),
                    row["observation_key"],
                )
                if current is not None and order <= current:
                    raise _refuse(
                        f"the {which} dataset's rows are not in (symbol, day, key) order at "
                        f"{row['observation_key']}: the pair cannot be proven by an ordered merge"
                    )
                name, current, revisions = (row["symbol"], row["observation_key"]), order, {}
            if item.lineage is not None:
                revisions[row["revision_id"]] = (
                    evidence_record_bytes(item.lineage),
                    None if item.gap is None else evidence_record_bytes(item.gap),
                )
    if current is not None:
        yield current, revisions


def _require_evidence_lineage(
    verifier: StreamingEvidenceVerifier,
    feature: ResearchDatasetEvidenceManifest,
    price: ResearchDatasetEvidenceManifest,
) -> None:
    """Step 9 by merge (module docs)."""
    f_listing, p_listing = _merge_listing(
        verifier,
        feature,
        price,
        EvidenceStream.LINEAGE,
        "price revision {revision} was never seen by the feature view",
    )
    canonical = rules.CANONICAL_TABLES[feature.data_type].table
    f_tables = {canonical, *((_LISTINGS,) if f_listing else ())}
    p_tables = {canonical, *((_LISTINGS,) if p_listing else ())}
    if f_tables != p_tables:
        raise _refuse(f"Canonical tables differ: {sorted(f_tables)} != {sorted(p_tables)}")
    _merge_listing(
        verifier,
        feature,
        price,
        EvidenceStream.EVIDENCE_GAPS,
        "evidence gaps of ['{revision}'] are bound by the price manifest only",
    )

    catalog = evidence_catalog(verifier)
    with (
        iter_dataset_chunks(catalog, feature, verifier) as f_chunks,
        iter_dataset_chunks(catalog, price, verifier) as p_chunks,
    ):
        f_groups = _Ahead(_key_groups(f_chunks, "feature"))
        for key, revisions in _key_groups(p_chunks, "price"):
            while (found := f_groups.peek()) is not _END and found[0] < key:
                f_groups.take()
            seen: _GroupRevisions = {} if found is _END or found[0] != key else found[1]
            for revision, (lineage, gap) in sorted(revisions.items()):
                if seen.get(revision, (None, None))[0] != lineage:
                    raise _refuse(f"price revision {revision} was never seen by the feature view")
                if gap is not None and seen[revision][1] != gap:
                    raise _refuse(
                        f"evidence gaps of {[revision]} are bound by the price manifest only"
                    )

    with ExitStack() as stack:
        f_reports = stack.enter_context(
            iter_manifest_evidence(verifier, feature, EvidenceStream.QUALITY_REPORTS)
        )
        p_reports = stack.enter_context(
            iter_manifest_evidence(verifier, price, EvidenceStream.QUALITY_REPORTS)
        )
        while True:
            f_report, p_report = next(f_reports, _END), next(p_reports, _END)
            if f_report is _END and p_report is _END:
                return
            if not (
                isinstance(f_report, DatasetQualityReportRef)
                and isinstance(p_report, DatasetQualityReportRef)
                and (f_report.report_id, f_report.sort_key())
                == (p_report.report_id, p_report.sort_key())
            ):
                raise _refuse("quality report ids differ")
