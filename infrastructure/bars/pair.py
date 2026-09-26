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

The pair hash binds the two manifest hashes under this rule's own hash (``PAIR_RULE_HASH``), so a
change of rule is a change of pair.

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
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import ResearchDatasetManifest, UniverseExclusion, UniverseMember
from core.domain.base import canonical_json, content_hash
from infrastructure.bars.dataset import DatasetBarsError
from infrastructure.bars.verified import VerifiedManifestCache, load_verified_manifest
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.pit.assumption import assumption_bound

__all__ = [
    "PAIR_RULE",
    "PAIR_RULE_HASH",
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


class ManifestPairError(DatasetBarsError):
    """The two manifests do not describe the same market data of one chain (fail closed)."""


@dataclass(frozen=True, slots=True)
class ManifestPair:
    """The verified pairing of one chain's feature (interval) and price (point) manifests."""

    feature_manifest_hash: str
    price_manifest_hash: str
    pair_hash: str

    def __post_init__(self) -> None:
        if self.pair_hash != pair_hash_of(self.feature_manifest_hash, self.price_manifest_hash):
            raise ManifestPairError("pair_hash does not bind these two manifest hashes")


def pair_manifests(
    builder: DatasetBuilder,
    feature_manifest_hash: str,
    price_manifest_hash: str,
    *,
    manifest_cache: VerifiedManifestCache | None = None,
) -> ManifestPair:
    """Load and verify both manifests, prove they describe the same market data, or refuse."""
    feature = load_verified_manifest(builder, feature_manifest_hash, manifest_cache)
    price = load_verified_manifest(builder, price_manifest_hash, manifest_cache)
    _require_pair(feature, price)
    return ManifestPair(
        feature_manifest_hash=feature.content_hash(),
        price_manifest_hash=price.content_hash(),
        pair_hash=pair_hash_of(feature.content_hash(), price.content_hash()),
    )


def pair_hash_of(feature_manifest_hash: str, price_manifest_hash: str) -> str:
    """The pair hash binding these two manifest hashes under ``PAIR_RULE_HASH``.

    Public so a consumer holding only a ``ManifestPair`` record (no builder, e.g. the research
    validator) can re-check that its ``pair_hash`` binds its two manifest hashes; that re-check
    is not a re-proof of the pairing itself (only ``pair_manifests`` proves it).
    """
    return content_hash(
        {
            "rule": PAIR_RULE_HASH,
            "feature_manifest": feature_manifest_hash,
            "price_manifest": price_manifest_hash,
        }
    )


# ---------------------------------------------------------------------------------------------


def _refuse(message: str) -> ManifestPairError:
    return ManifestPairError(f"manifest pair refused: {message}")


def _require_pair(feature: ResearchDatasetManifest, price: ResearchDatasetManifest) -> None:
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
    _require_instruments(feature, price, start, end)  # step 8
    _require_lineage(feature, price)  # step 9


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
