"""Streaming verifier of v3 Research Dataset evidence manifests (ADR-0077 §6 / §6.3, B4).

``StreamingEvidenceVerifier.verify_evidence_manifest`` is the ``EvidenceManifestVerifier`` of
``DatasetEvidenceManifestStore`` (called before persist and after load, as the v2
``DatasetBuilder.verify_manifest``). It proves a ``ResearchDatasetEvidenceManifest`` is exactly
what a bounded build of its own inputs produces, and never holds a collection that grows with the
rows, the keys, the window or the universe:

1. **identity** (§6.1): the manifest re-validates to itself; its rule is the verifier's rule
   (``hlens.dataset.pit-selection@2.1.0`` with these parameters: DQ-9 is OPEN, so there is no
   registry of parameter values, only the rule the verifier was configured with); its universe is
   a registered spec with this binding; its data type is known; its dataset table is the chunk
   table; its ``selection_id`` is recomputed from its own inputs;
2. **re-derivation, merged record by record** (§6.2): ``DatasetEvidenceBuilder.select`` runs the
   same bounded generator inside the manifest's recorded contract version (ADR-0052 V7) into a
   *comparing sink*. The five derived streams are opened with ``iter_evidence`` (root-hash
   authenticated) and advanced in lockstep: every pushed record must be byte-equal (the unique
   evidence projection) to the next stored one, and both sides must run out together. Rows are
   grouped into chunks of ``chunk_rows`` and merged against the ``chunk_proofs`` stream;
3. **chunks** (§4 / §6.2 / §6.4): per chunk, the proof names batch
   ``<selection_id>.chunk-<i>``, ``first_row_ordinal = i x chunk_rows``, the chunk's row count and
   the ``hlens.pyarrow-batch-sha256@1.0.0`` fingerprint of the derived chunk (Arrow Table of the
   chunk table's schema, rows by ``row_ordinal``); its snapshot commits exactly that batch,
   fingerprint and row count; the chunk read back at the manifest's dataset snapshot
   (``selection_id`` and ``chunk_index`` filtered, bounded scan, sorted by ``row_ordinal`` within
   the chunk) equals the derived chunk. The last proof's snapshot is the manifest's
   ``dataset.snapshot_id``; the dataset snapshot holds exactly ``row_count`` rows of the selection
   (streamed count); ``DatasetChunkWriter.seal`` proves no chunk ``>= chunk_count`` exists;
4. **semantic checks v2 did with sets, done by ordered merge** (§6.3):

   - *every member / exclusion listing revision has listing lineage, and belongs to the episode
     citing it*: claims ``(listing_revision_id, episode key)`` are buffered in a claim buffer
     closed like an evidence leaf (``leaf_max_records`` / ``leaf_max_bytes`` of the rule), sorted,
     resolved against the listing table at the bound snapshot (``ListingEpisodes``: the
     revision's own ``observation_key`` must equal the citing episode's, so two episodes can
     never claim one revision) and merged against a fresh pass over the listing prefix of the
     stored ``lineage`` stream (sorted by revision id);
   - *every evidence gap cites a report of the quality_reports stream, of its own partition*:
     listing gaps the listing report, a data gap the ``(venue symbol, UTC day)`` report of the row
     it follows; claims are buffered and merged the same way against the stored
     ``quality_reports`` stream (sorted by ``sort_key``);
   - every lineage / gap table is bound by the PIT spec.

   Everything else v2 checks (member / exclusion conflicts and overlaps, key order and slice
   ownership, PIT conflicts, reports re-derived ``existing_only``, gaps recorded by their report,
   the unbound-table refusal) is re-checked by the generator itself during the re-derivation.

Working set: the generator's own (one key group, one symbol's member spans, one cached report),
one chunk of derived rows plus the same chunk read back, one object and a ``depth x fanout``
reference stack per open evidence stream (six in lockstep, plus at most one claim pass), and two
claim buffers bounded like one evidence leaf each. The v2 path (``DatasetBuilder
.verify_manifest``) is materializing and not part of this claim (§8.3); E1-CAP-1 is not implied.

``DatasetChunkWriter`` is used for its ``table`` and its read-only ``seal``; chunk content is read
directly from the chunk table through the bounded ``scan_column_batches`` (ADR-0075), so the
verifier depends on B3 only through the ``DatasetChunkWriter`` Protocol.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC
from typing import Any, Final, NoReturn, Protocol

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import And, EqualTo, In

from core.contracts.catalog import SnapshotNotFound
from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
    dataset_chunk_batch_id,
)
from core.domain.base import Contract, contract_schema_version_scope
from infrastructure import contract_version
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    CANONICAL_INSTRUMENT_LISTINGS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset.builder import (
    DatasetChunkWriter,
    DatasetDerivation,
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    DatasetSpecError,
)
from infrastructure.dataset.evidence import (
    EVIDENCE_RECORD_TYPES,
    EvidenceTreeLimits,
    evidence_record_bytes,
)
from infrastructure.dataset.manifests import DatasetEvidenceManifestStore
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe.builder import REGISTERED_UNIVERSES

__all__ = [
    "DatasetEvidenceSourcesFactory",
    "ListingEpisodes",
    "PinnedListingEpisodes",
    "StreamingEvidenceVerifier",
]

_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_CHUNKS: Final = DATASET_SELECTION_CHUNKS
_CHUNK_COLUMNS: Final = tuple(field.name for field in DATASET_SELECTION_CHUNKS.arrow_schema)
#: Canonical symbol -> venue symbol of the first-slice instrument registry (a code constant).
_VENUE_SYMBOL: Final[Mapping[str, str]] = {
    instrument.symbol: venue_symbol for venue_symbol, instrument in rules.SYMBOLS.items()
}
_LISTING_REPORT_KEY: Final = (0, "", "")
_END: Final = object()

#: Builds the ordered input cursors of a request (B-UNIV / B-PIT / quality at the bound snapshots).
DatasetEvidenceSourcesFactory = Callable[[DatasetEvidenceRequest], DatasetEvidenceSources]


class ListingEpisodes(Protocol):
    """The episode (``observation_key``) of listing revisions at a PIT spec's bound snapshot."""

    def observation_keys(
        self, pit: PointInTimeSpec, revision_ids: Sequence[str]
    ) -> Iterator[tuple[str, str]]:
        """``(revision_id, observation_key)`` of every listing row among ``revision_ids``
        (at most ``len(revision_ids)`` ids per call; each row once, absent ones omitted)."""
        ...


class PinnedListingEpisodes:
    """``ListingEpisodes`` over ``canonical.instrument_listings`` at the PIT spec's snapshot."""

    def __init__(self, adapter: RevisionCatalog) -> None:
        self._adapter = adapter

    def observation_keys(
        self, pit: PointInTimeSpec, revision_ids: Sequence[str]
    ) -> Iterator[tuple[str, str]]:
        snapshot = pit.snapshot_bindings.get(_LISTINGS)
        if snapshot is None:
            raise CatalogIntegrityError(f"the PIT spec does not bind {_LISTINGS}")
        if not revision_ids:
            return
        batches = self._adapter.scan_column_batches(
            _LISTINGS,
            columns=("revision_id", "observation_key"),
            row_filter=In("revision_id", tuple(revision_ids)),  # type: ignore[call-arg, arg-type]
            snapshot_id=snapshot,
        )
        try:
            for batch in batches:
                for row in batch.to_pylist():
                    yield row["revision_id"], row["observation_key"]
        finally:
            _close(batches)


def _close(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


# =========================================================================================
# the verifier
# =========================================================================================


class StreamingEvidenceVerifier:
    """Proves v3 manifests by a bounded re-derivation merged against their stored evidence."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        *,
        builder: DatasetEvidenceBuilder,
        chunks: DatasetChunkWriter,
        sources: DatasetEvidenceSourcesFactory,
        listing_episodes: ListingEpisodes | None = None,
    ) -> None:
        if not isinstance(builder, DatasetEvidenceBuilder):
            raise DatasetSpecError("builder must be a DatasetEvidenceBuilder")
        if chunks.table != _CHUNKS.table:
            raise DatasetSpecError(f"the chunk table must be {_CHUNKS.table}, not {chunks.table}")
        self._adapter = adapter
        self._builder = builder
        self._chunks = chunks
        self._sources = sources
        self._episodes: ListingEpisodes = (
            PinnedListingEpisodes(adapter) if listing_episodes is None else listing_episodes
        )

    @property
    def adapter(self) -> RevisionCatalog:
        """The catalog this verifier proves v3 manifests of (public; C1-CONSUMERS follow-up)."""
        return self._adapter

    @property
    def builder(self) -> DatasetEvidenceBuilder:
        """The v3 evidence reader this verifier re-derives builds with (public; same follow-up)."""
        return self._builder

    def store(self) -> DatasetEvidenceManifestStore:
        """The v3 manifest store whose manifests this verifier proves (persist and load)."""
        return DatasetEvidenceManifestStore(self._adapter, self)

    def verify_evidence_manifest(
        self, manifest: ResearchDatasetEvidenceManifest, *, manifested: bool
    ) -> None:
        """``manifest`` is exactly what a build of its own inputs produced, or raise."""
        request = self._request(manifest)
        version = contract_version.replay_version(
            manifest.schema_version, what=f"evidence manifest {manifest.content_hash()}"
        )
        limits = self._builder.rule.limits
        with contract_schema_version_scope(version), ExitStack() as stack:
            stored = {
                ref.stream: stack.enter_context(
                    self._builder.iter_evidence(manifest, ref.stream)
                )
                for ref in manifest.evidence
            }

            def reopen(stream: EvidenceStream) -> AbstractContextManager[Iterator[Contract]]:
                return self._builder.iter_evidence(manifest, stream)

            sink = _ComparingSink(
                manifest,
                stored,
                listing=_ListingClaims(limits, manifest.point_in_time, self._episodes, reopen),
                reports=_ReportClaims(limits, reopen),
                chunks=_ChunkComparer(
                    self._adapter, manifest, stored[EvidenceStream.CHUNK_PROOFS]
                ),
            )
            derived = self._builder.select(
                request,
                sources=self._sources(request),
                sink=sink,
                manifested=manifested,
                schema_version=manifest.schema_version,
            )
            sink.finish(derived)
        self._no_extra_rows(manifest)
        self._chunks.seal(manifest.selection_id, manifest.chunk_count)

    # ------------------------------------------------------------------ identity

    def _request(self, manifest: ResearchDatasetEvidenceManifest) -> DatasetEvidenceRequest:
        if not isinstance(manifest, ResearchDatasetEvidenceManifest):
            raise DatasetSpecError("manifest must be a ResearchDatasetEvidenceManifest")
        again = ResearchDatasetEvidenceManifest.model_validate_json(manifest.model_dump_json())
        if again.content_hash() != manifest.content_hash():
            raise CatalogIntegrityError("the evidence manifest does not re-validate to itself")
        rule = self._builder.rule
        if not rule.binds(manifest.rule) or manifest.chunk_rows != rule.chunk_rows:
            raise CatalogIntegrityError(
                f"manifest rule {manifest.rule.rule_id}@{manifest.rule.version} "
                f"({manifest.rule.rule_hash}) is not the verifier's rule"
            )
        binding = manifest.universe_spec
        universe = REGISTERED_UNIVERSES.get((binding.name, binding.version))
        if universe is None or universe.binding() != binding:
            raise CatalogIntegrityError(
                f"manifest universe {binding.name}@{binding.version} is not a registered spec"
            )
        if manifest.data_type not in rules.CANONICAL_TABLES:
            raise CatalogIntegrityError(f"manifest data type {manifest.data_type!r} is unknown")
        dataset = manifest.dataset
        if dataset.table != self._chunks.table:
            raise CatalogIntegrityError(
                f"manifest dataset {dataset.table} is not the chunk table {self._chunks.table}"
            )
        # §4.3: the dataset snapshot is the one committing the last chunk.
        try:
            snapshot = self._adapter.get_snapshot(dataset.table, dataset.snapshot_id)
        except SnapshotNotFound as exc:
            raise CatalogIntegrityError(
                f"manifest dataset snapshot {dataset.snapshot_id} is not a snapshot of "
                f"{dataset.table}"
            ) from exc
        if snapshot.batch_id != manifest.chunk_batch_id(manifest.chunk_count - 1):
            raise CatalogIntegrityError(
                f"manifest dataset snapshot {dataset.snapshot_id} does not commit the last chunk "
                f"of {manifest.selection_id}"
            )
        request = DatasetEvidenceRequest(
            universe=universe,
            pit=manifest.point_in_time,
            data_type=manifest.data_type,
            start=dataset.time_range_start,
            end=dataset.time_range_end,
        )
        if self._builder.selection_id(request) != manifest.selection_id:
            raise CatalogIntegrityError(
                f"manifest selection {manifest.selection_id} is not the selection of its own "
                "inputs"
            )
        return request

    # ------------------------------------------------------------------ no extra data (§6.4)

    def _no_extra_rows(self, manifest: ResearchDatasetEvidenceManifest) -> None:
        batches = self._adapter.scan_column_batches(
            _CHUNKS.table,
            columns=("row_ordinal",),
            row_filter=EqualTo("selection_id", manifest.selection_id),  # type: ignore[call-arg, arg-type]
            snapshot_id=manifest.dataset.snapshot_id,
        )
        total = 0
        try:
            for batch in batches:
                total += batch.num_rows
                if total > manifest.row_count:
                    break
        finally:
            _close(batches)
        if total != manifest.row_count:
            raise CatalogIntegrityError(
                f"dataset snapshot {manifest.dataset.snapshot_id} holds {total} rows of "
                f"{manifest.selection_id}, not its {manifest.row_count}"
            )


# =========================================================================================
# the comparing sink
# =========================================================================================


class _ComparingSink:
    """``DatasetDerivationSink`` merging every derived item with the stored evidence in lockstep.

    Holds the stored iterators (one object each), the last derived row (a data gap's partition)
    and per-stream ordinals; claims and chunks go to their bounded checkers.
    """

    def __init__(
        self,
        manifest: ResearchDatasetEvidenceManifest,
        stored: Mapping[EvidenceStream, Iterator[Contract]],
        *,
        listing: _ListingClaims,
        reports: _ReportClaims,
        chunks: _ChunkComparer,
    ) -> None:
        self._manifest = manifest
        self._stored = stored
        self._bound = manifest.point_in_time.snapshot_bindings
        self._canonical = rules.CANONICAL_TABLES[manifest.data_type].table
        self._listing = listing
        self._reports = reports
        self._chunks = chunks
        self._streams = tuple(
            ref.stream
            for ref in manifest.evidence
            if ref.stream is not EvidenceStream.CHUNK_PROOFS
        )
        self._ordinals = dict.fromkeys(self._streams, 0)
        self._last_row: Mapping[str, Any] | None = None

    def evidence(self, stream: EvidenceStream, record: Contract) -> None:
        stream = EvidenceStream(stream)
        if stream is EvidenceStream.CHUNK_PROOFS:
            raise CatalogIntegrityError("chunk proofs come from chunk commits only")
        ordinal = self._ordinals[stream]
        if type(record) is not EVIDENCE_RECORD_TYPES[stream]:
            raise CatalogIntegrityError(f"{stream.value} record {ordinal} has the wrong type")
        stored = next(self._stored[stream], _END)
        if stored is _END:
            raise CatalogIntegrityError(
                f"{stream.value}: the re-derivation yields record {ordinal}, the manifest "
                "commits no more"
            )
        if not isinstance(stored, Contract):  # pragma: no cover - iter_evidence yields records
            raise CatalogIntegrityError(f"{stream.value} stored record {ordinal} is malformed")
        if evidence_record_bytes(stored) != evidence_record_bytes(record):
            raise CatalogIntegrityError(
                f"{stream.value} record {ordinal} is not what the manifest's inputs derive"
            )
        self._ordinals[stream] = ordinal + 1
        self._check(stream, record)

    def row(self, row: Mapping[str, Any]) -> None:
        self._last_row = row
        self._chunks.add(row)

    def finish(self, derived: DatasetDerivation) -> None:
        """Both sides ran out together; counts agree with the manifest; claims resolved."""
        for stream in self._streams:
            if next(self._stored[stream], _END) is not _END:
                raise CatalogIntegrityError(
                    f"{stream.value}: the manifest commits more than the "
                    f"{self._ordinals[stream]} records its inputs derive"
                )
        counts = dict(derived.record_counts)
        for stream in self._streams:
            committed = self._manifest.evidence_for(stream).record_count
            if counts.get(stream) != committed or self._ordinals[stream] != committed:
                raise CatalogIntegrityError(
                    f"{stream.value}: {counts.get(stream)} records derived, {committed} committed"
                )
        if derived.selection_id != self._manifest.selection_id:
            raise CatalogIntegrityError("the re-derivation is of another selection")
        if derived.row_count != self._manifest.row_count:
            raise CatalogIntegrityError(
                f"{derived.row_count} rows derived, the manifest commits {self._manifest.row_count}"
            )
        self._listing.close()
        self._reports.close()
        self._chunks.close()

    # ------------------------------------------------------------------ semantic checks (§6.3)

    def _check(self, stream: EvidenceStream, record: Contract) -> None:
        if isinstance(record, UniverseMember | UniverseExclusion):
            self._listing.claim(record.listing_revision_id, record.episode.observation_key())
        elif isinstance(record, SelectedRevisionLineage):
            for table in (record.canonical_table, record.raw_table, record.source_table):
                if table not in self._bound:
                    raise CatalogIntegrityError(
                        f"lineage cites {table}, which the PIT spec does not bind"
                    )
        elif isinstance(record, AvailabilityEvidenceGap):
            self._gap(record)

    def _gap(self, gap: AvailabilityEvidenceGap) -> None:
        if gap.table not in self._bound:
            raise CatalogIntegrityError(f"evidence gap cites {gap.table}, which is not bound")
        if gap.table == _LISTINGS:
            self._reports.claim(gap.quality_report_id, _LISTING_REPORT_KEY)
            return
        row = self._last_row
        if (
            gap.table != self._canonical
            or row is None
            or row["revision_id"] != gap.revision_id
            or row["canonical_table"] != gap.table
        ):
            raise CatalogIntegrityError(
                f"evidence gap of {gap.table} revision {gap.revision_id} does not follow its row"
            )
        venue_symbol = _VENUE_SYMBOL.get(row["symbol"])
        if venue_symbol is None:
            raise CatalogIntegrityError(f"row symbol {row['symbol']!r} is not a known instrument")
        day = row["event_time"].astimezone(UTC).date()
        self._reports.claim(gap.quality_report_id, (1, venue_symbol, day.isoformat()))


# =========================================================================================
# bounded claim buffers (ordered merge instead of whole-dataset sets)
# =========================================================================================


class _ClaimBuffer[K: (str, tuple[int, str, str])]:
    """Claims ``(key, value)`` buffered like an evidence leaf, then sorted and resolved.

    The buffer closes when it holds ``leaf_max_records`` claims or the next claim would take its
    UTF-8 size over ``leaf_max_bytes`` (a single larger claim is resolved alone): its working set
    is that of one leaf. Resolution merges the sorted claims with one fresh pass over a stored
    stream sorted by the same key.
    """

    def __init__(self, limits: EvidenceTreeLimits) -> None:
        self._limits = limits
        self._items: list[tuple[K, str]] = []
        self._bytes = 0
        self.passes = 0

    def _add(self, key: K, value: str, size: int) -> None:
        if self._items and (
            len(self._items) == self._limits.leaf_max_records
            or self._bytes + size > self._limits.leaf_max_bytes
        ):
            self._flush()
        self._items.append((key, value))
        self._bytes += size

    def close(self) -> None:
        if self._items:
            self._flush()

    def _flush(self) -> None:
        items = sorted(set(self._items))
        self._items = []
        self._bytes = 0
        claims: list[tuple[K, str]] = []
        for key, group in itertools.groupby(items, key=lambda item: item[0]):
            values = {value for _, value in group}
            if len(values) != 1:
                self._conflict(key, sorted(values))
            claims.append((key, values.pop()))
        self.passes += 1
        self._resolve(claims)

    def _conflict(self, key: K, values: list[str]) -> NoReturn:
        raise NotImplementedError  # pragma: no cover

    def _resolve(self, claims: list[tuple[K, str]]) -> None:
        raise NotImplementedError  # pragma: no cover


class _ListingClaims(_ClaimBuffer[str]):
    """Each cited listing revision: its episode is the citing one; it has listing lineage."""

    def __init__(
        self,
        limits: EvidenceTreeLimits,
        pit: PointInTimeSpec,
        episodes: ListingEpisodes,
        reopen: Callable[[EvidenceStream], AbstractContextManager[Iterator[Contract]]],
    ) -> None:
        super().__init__(limits)
        self._pit = pit
        self._episodes = episodes
        self._reopen = reopen

    def claim(self, revision_id: str, observation_key: str) -> None:
        size = len(revision_id.encode("utf-8")) + len(observation_key.encode("utf-8"))
        self._add(revision_id, observation_key, size)

    def _conflict(self, key: str, values: list[str]) -> NoReturn:
        raise CatalogIntegrityError(f"listing revision {key} is claimed by {len(values)} episodes")

    def _resolve(self, claims: list[tuple[str, str]]) -> None:
        wanted = dict(claims)  # bounded by one claim buffer
        found: dict[str, str] = {}
        for revision, key in self._episodes.observation_keys(self._pit, list(wanted)):
            if revision not in wanted:
                raise CatalogIntegrityError(f"listing lookup answered unasked revision {revision}")
            if revision in found:
                raise CatalogIntegrityError(f"listing revision {revision} has two listing rows")
            found[revision] = key
        for revision, key in claims:
            if revision not in found:
                raise CatalogIntegrityError(
                    f"listing revision {revision} is not in {_LISTINGS} at the bound snapshot"
                )
            if found[revision] != key:
                raise CatalogIntegrityError(
                    f"listing revision {revision} is of another episode than the one citing it"
                )
        pending = iter(revision for revision, _ in claims)
        want = next(pending, None)
        with self._reopen(EvidenceStream.LINEAGE) as records:
            for record in records:
                if want is None:
                    break
                if not isinstance(record, SelectedRevisionLineage):  # pragma: no cover
                    raise CatalogIntegrityError("a lineage record is malformed")
                if record.canonical_table != _LISTINGS:
                    break  # the listing prefix is over (ADR-0077 §2)
                revision = record.canonical_revision_id
                if want < revision:
                    break
                if want == revision:
                    want = next(pending, None)
        if want is not None:
            raise CatalogIntegrityError(
                f"listing revision {want} cited by the universe has no listing lineage"
            )


class _ReportClaims(_ClaimBuffer[tuple[int, str, str]]):
    """Each gap's report: the ``quality_reports`` record of its partition, with that id."""

    def __init__(
        self,
        limits: EvidenceTreeLimits,
        reopen: Callable[[EvidenceStream], AbstractContextManager[Iterator[Contract]]],
    ) -> None:
        super().__init__(limits)
        self._reopen = reopen

    def claim(self, report_id: str, key: tuple[int, str, str]) -> None:
        size = len(report_id.encode("utf-8")) + len(key[1].encode("utf-8")) + len(key[2])
        self._add(key, report_id, size)

    def _conflict(self, key: tuple[int, str, str], values: list[str]) -> NoReturn:
        raise CatalogIntegrityError(f"evidence gaps of partition {key} cite {len(values)} reports")

    def _resolve(self, claims: list[tuple[tuple[int, str, str], str]]) -> None:
        pending = iter(claims)
        want = next(pending, None)
        with self._reopen(EvidenceStream.QUALITY_REPORTS) as records:
            for record in records:
                if want is None:
                    break
                if not isinstance(record, DatasetQualityReportRef):  # pragma: no cover
                    raise CatalogIntegrityError("a quality report record is malformed")
                key = record.sort_key()
                if want[0] < key:
                    break
                if want[0] == key:
                    if record.report_id != want[1]:
                        raise CatalogIntegrityError(
                            f"an evidence gap cites report {want[1]}, the quality_reports stream "
                            f"binds {record.report_id} for its partition"
                        )
                    want = next(pending, None)
        if want is not None:
            raise CatalogIntegrityError(
                f"an evidence gap cites report {want[1]}, which the quality_reports stream does "
                "not bind for its partition"
            )


# =========================================================================================
# chunks
# =========================================================================================


class _ChunkComparer:
    """Groups derived rows into chunks and proves each against its proof and the chunk table."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        manifest: ResearchDatasetEvidenceManifest,
        proofs: Iterator[Contract],
    ) -> None:
        self._adapter = adapter
        self._manifest = manifest
        self._proofs = proofs
        self._rows: list[Mapping[str, Any]] = []
        self._index = 0
        self._last_snapshot: str | None = None

    def add(self, row: Mapping[str, Any]) -> None:
        self._rows.append(row)
        if len(self._rows) == self._manifest.chunk_rows:
            self._check()

    def close(self) -> None:
        if self._rows:
            self._check()
        if next(self._proofs, _END) is not _END:
            raise CatalogIntegrityError(
                f"the manifest proves more chunks than the {self._index} its inputs derive"
            )
        if self._index != self._manifest.chunk_count:
            raise CatalogIntegrityError(  # pragma: no cover - the proof stream count proves it
                f"{self._index} chunks derived, the manifest commits {self._manifest.chunk_count}"
            )
        if self._last_snapshot != self._manifest.dataset.snapshot_id:
            raise CatalogIntegrityError(
                f"the dataset snapshot {self._manifest.dataset.snapshot_id} is not the snapshot "
                f"of the last chunk ({self._last_snapshot})"
            )

    def _check(self) -> None:
        index, rows = self._index, self._rows
        self._rows = []
        manifest = self._manifest
        selection_id = manifest.selection_id
        proof = next(self._proofs, _END)
        if proof is _END:
            raise CatalogIntegrityError(
                f"the inputs derive chunk {index}, the manifest proves no more chunks"
            )
        if not isinstance(proof, DatasetChunkProof):  # pragma: no cover - the stream's model
            raise CatalogIntegrityError(f"chunk proof {index} is malformed")
        first = index * manifest.chunk_rows
        for offset, row in enumerate(rows):
            if (row["chunk_index"], row["row_ordinal"]) != (index, first + offset):
                raise CatalogIntegrityError(f"derived chunk {index} rows are not contiguous")
        expected = pa.Table.from_pylist([dict(row) for row in rows], schema=_CHUNKS.arrow_schema)
        fingerprint = _CHUNKS.fingerprint_rule.fingerprint(expected)
        batch_id = dataset_chunk_batch_id(selection_id, index)
        if (
            proof.chunk_index,
            proof.batch_id,
            proof.first_row_ordinal,
            proof.row_count,
            proof.batch_fingerprint,
        ) != (index, batch_id, first, len(rows), fingerprint):
            raise CatalogIntegrityError(
                f"chunk proof {index} of {selection_id} does not prove the derived chunk"
            )
        try:
            snapshot = self._adapter.get_snapshot(_CHUNKS.table, proof.snapshot_id)
        except SnapshotNotFound as exc:
            raise CatalogIntegrityError(
                f"chunk {index} snapshot {proof.snapshot_id} is not a snapshot of {_CHUNKS.table}"
            ) from exc
        if (snapshot.batch_id, snapshot.batch_fingerprint, snapshot.added_rows) != (
            batch_id,
            fingerprint,
            len(rows),
        ):
            raise CatalogIntegrityError(
                f"snapshot {proof.snapshot_id} does not commit chunk {index} of {selection_id}"
            )
        if self._read_back(index, len(rows)) != expected.to_pylist():
            raise CatalogIntegrityError(
                f"chunk {index} of {selection_id} reads back differently at the dataset snapshot "
                f"{manifest.dataset.snapshot_id}"
            )
        self._index += 1
        self._last_snapshot = proof.snapshot_id

    def _read_back(self, index: int, count: int) -> list[dict[str, Any]]:
        """The chunk's rows at the dataset snapshot, by ``row_ordinal`` (at most ``count + 1``
        are ever held: one more is already a mismatch)."""
        manifest = self._manifest
        batches = self._adapter.scan_column_batches(
            _CHUNKS.table,
            columns=_CHUNK_COLUMNS,
            row_filter=And(
                EqualTo("selection_id", manifest.selection_id),  # type: ignore[call-arg, arg-type]
                EqualTo("chunk_index", index),  # type: ignore[call-arg, arg-type]
            ),
            snapshot_id=manifest.dataset.snapshot_id,
        )
        found: list[dict[str, Any]] = []
        try:
            for batch in batches:
                if len(found) + batch.num_rows > count:
                    raise CatalogIntegrityError(
                        f"chunk {index} of {manifest.selection_id} holds more than its {count} "
                        f"rows at {manifest.dataset.snapshot_id}"
                    )
                found.extend(batch.to_pylist())
        finally:
            _close(batches)
        found.sort(key=lambda row: row["row_ordinal"])
        return found
