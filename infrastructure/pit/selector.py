"""Point-in-time selection over Canonical revisions (Phase 1 F1; ADR-0023 §5, ADR-0028 §3.2 / §7).

``PitSelector.select(spec, data_type, symbol, start, end)`` answers, for every Canonical
``observation_key`` of one venue symbol whose event lies in the UTC window ``[start, end)``,
what a dataset bound to ``spec`` may use:

1. **bindings** — the spec's PIT rule must be ``hlens.pit.maximal-head@1.0.0``; its availability,
   precedence and parser bindings must include, with exact hashes, the Canonical availability
   policy, the D-33 channel policy, the Canonical precedence map and the normalizer. The
   Canonical table must be bound. Anything else is ``PitSpecError`` (fail closed);
2. **pinned, proven reads** — everything is read through a ``PinnedCatalogView`` of the spec's
   snapshot bindings. Every Canonical row is proven by re-normalizing its Raw unit
   (``CanonicalNormalizer.verify_unit``, whose Raw rows ``PersistedRowVerifier`` proves; only the
   committed batches holding rows that were read, plus the unit-wide facts, G3-S2), and every
   Raw ``archive → REST`` edge of the window by the reconciler's re-derivation
   (``ChannelReconciler.verified_edges``). Anything that does not reproduce is
   ``CatalogIntegrityError``;
3. **mapping** — each verified Raw edge becomes a Canonical ``PrecedenceEvidence`` through
   ``rules.map_channel_edge`` when both Raw endpoints have exactly one Canonical revision in the
   bound snapshot (zero: no Canonical edge; more: fail closed);
4. **selection** — per key: revisions and edges with ``knowledge_time <= knowledge_cutoff`` form
   the graph (validated as a ``RevisionGraph``); candidates also need ``available_time <= t``;
   ``maximal_heads`` keeps the candidates no other candidate supersedes, directly or through any
   known revision. One head: selected; none: absent; more: conflict. ``arrival_seq``, wall
   clocks and payload hashes are never read for a decision.

For a simulation interval the result is evaluated at the interval start and at every
``available_time`` of a known revision inside the interval (the only instants a selection can
change); consecutive identical results are merged. The output carries each selection, the
``SelectedRevisionLineage`` of every selected revision and its availability evidence gap; a
dataset builder must refuse ``conflicts`` (``require_no_conflict``).
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
from pyiceberg.expressions import And, BooleanExpression, EqualTo, GreaterThanOrEqual, LessThan

from core.contracts.revision import (
    PointInTimeSelection,
    PointInTimeSpec,
    PointInTimeStatus,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.contracts.storage import StorageAdapter
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import canonical_json
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_PRECEDENCE_EVIDENCE
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.pit.assumption import (
    AssumptionSpecError,
    assumption_bound,
    effective_available_times,
)
from infrastructure.pit.runs import (
    RunLimits,
    RunRef,
    RunSetBuilder,
    iter_run,
)
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING, ChannelEdge
from infrastructure.revision.channel_reconcile import (
    ChannelReconciler,
    evidence_from_row,
    revision_record_from_row,
)
from infrastructure.revision.precedence import maximal_heads
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "PIT_BINDING",
    "PIT_SPEC",
    "REQUIRED_BINDINGS",
    "EvidenceGap",
    "PitBoundedRecord",
    "PitConflictError",
    "PitRunParams",
    "PitSelection",
    "PitSelector",
    "PitSpecError",
]

_DAY: Final = timedelta(days=1)
_HOUR: Final = timedelta(hours=1)
#: Rows (all columns) one fetch of the key-closure read may hold before filtering (G3-P).
_FETCH_ROWS: Final = 100_000
#: How far from a window the other revisions of its keys are looked for (key closure).
_KEY_REACH: Final = timedelta(days=1)
#: Bound on the transitive widening of that search (a longer chain fails closed).
_CLOSURE_STEPS: Final = 16

PIT_RULE_ID: Final = "hlens.pit.maximal-head"
PIT_RULE_VERSION: Final = "1.0.0"
PIT_SPEC: Final[dict[str, Any]] = {
    "rule": PIT_RULE_ID,
    "version": PIT_RULE_VERSION,
    "adr": ["ADR-0023 §5", "ADR-0028 §3.2 / §7"],
    "candidates": "available_time <= simulation_time AND knowledge_time <= knowledge_cutoff",
    "graph": "revisions and edges with knowledge_time <= knowledge_cutoff; RevisionGraph-valid",
    "edges": "in-row supersedes + Raw archive->REST edges mapped one to one onto Canonical "
    "(hlens.canonical.precedence-map), verified at the bound snapshots",
    "elimination": "a candidate superseded, directly or through any known revision, by another "
    "candidate is eliminated",
    "result": "1 head -> selected; 0 -> absent; >1 -> conflict (the dataset fails closed)",
    "window": "a key's revisions chained by gaps of at most one day are evaluated together; the "
    "key belongs to the window holding the chain's earliest event (a revision more than one day "
    "from every other is its own chain)",
    "interval": "evaluated at the start and at every in-interval available_time of a known "
    "revision; identical consecutive results merged",
    "never_read": ["arrival_seq", "wall clock", "payload_hash ordering"],
    "unbound_tables": "read as empty (only removes information); the Canonical table must be bound",
    "availability_assumptions": "an assumption policy bound in availability_bindings "
    "(hlens.availability.archive-event-time-assumption, ADR-0032) replaces available_time, "
    "never later, for the revisions it names; stored revisions and evidence gaps unchanged",
    "evidence_binding": "an unbound Raw evidence table means no edges (conflicts only, never "
    "another selection); the dataset builder must bind it whenever it has a snapshot "
    "(ADR-0027 §13, ADR-0028 §7) and the result records whether it was bound",
}
PIT_HASH: Final = hashlib.sha256(canonical_json(PIT_SPEC).encode("utf-8")).hexdigest()
PIT_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.POINT_IN_TIME,
    policy_id=PIT_RULE_ID,
    version=PIT_RULE_VERSION,
    policy_hash=PIT_HASH,
)
#: Bindings a spec must carry (exact id + version + hash) to select Canonical revisions.
REQUIRED_BINDINGS: Final[Mapping[str, tuple[PolicyBinding, ...]]] = {
    "availability_bindings": (rules.AVAILABILITY_BINDING,),
    "precedence_bindings": (DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
    "parser_bindings": (rules.NORMALIZER_BINDING,),
}


class PitSpecError(ValueError):
    """The spec cannot drive this selection (bindings, scope); nothing is selected."""


@dataclass(frozen=True, slots=True)
class EvidenceGap:
    """An availability evidence gap of a selected revision (bound to a report by F3)."""

    table: str
    revision_id: str
    gap: str


@dataclass(frozen=True, slots=True)
class PitSelection:
    """The selection of one scope under one spec."""

    canonical_table: str
    selections: tuple[PointInTimeSelection, ...]
    lineage: tuple[SelectedRevisionLineage, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    #: Observation keys with at least one conflicting evaluation.
    conflicts: tuple[str, ...]
    #: The verified Canonical records per key (what the selections were computed from), exactly
    #: as stored: with the ADR-0032 assumption bound, their ``available_time`` is the stored one —
    #: the effective times are in ``assumed`` and ``selected_rows`` (never recompute visibility
    #: from these records).
    records: Mapping[str, tuple[RevisionRecord, ...]]
    edges: Mapping[str, tuple[PrecedenceEvidence, ...]]
    #: Whether the spec bound the Raw evidence table (False = read as no edges).
    evidence_bound: bool
    #: The proven Canonical row of every revision that is selected at some evaluation; with the
    #: ADR-0032 assumption bound, ``available_time`` is the effective one (see ``assumed``).
    selected_rows: Mapping[str, Mapping[str, Any]]
    #: ADR-0032: revision -> (stored, effective) available_time for every evaluated revision the
    #: bound assumption moved; empty when the spec does not bind it.
    assumed: Mapping[str, tuple[datetime, datetime]] = dataclass_field(default_factory=dict)

    def require_no_conflict(self) -> None:
        if self.conflicts:
            raise PitConflictError(
                f"{len(self.conflicts)} observation key(s) have competing heads, first "
                f"{self.conflicts[0]}: the dataset build fails closed"
            )


class PitConflictError(Exception):
    """A dataset cannot be built: competing maximal heads (ADR-0023 §5.4)."""


@dataclass(frozen=True, slots=True)
class PitRunParams:
    """Explicit, caller-chosen bounds for :meth:`PitSelector.iter_bounded` (ADR-0077 §6.1.2 /
    §6.1.3, DQ-9: no defaults — values must be chosen after capacity measurement, not guessed
    here).

    - ``row_batch_rows`` / ``edge_batch_rows``: how many verified Canonical rows, respectively
      mapped edges, ``iter_bounded`` buffers in memory before spilling them into one
      content-addressed sorted run (:func:`infrastructure.pit.runs.spill_sorted_runs`);
    - ``merge_fanout``: how many sorted runs :func:`infrastructure.pit.runs.merge_sorted_runs`
      reads from at once when reconstructing key order;
    - ``key_history_buffer``: the input capacity used while building one observation key's
      content-addressed history run;
    - ``limits``: the leaf / index shape (:class:`infrastructure.pit.runs.RunLimits`) used for
      every run this call writes, including the intermediate runs a large merge produces.
    """

    row_batch_rows: int
    edge_batch_rows: int
    merge_fanout: int
    key_history_buffer: int
    limits: RunLimits

    def __post_init__(self) -> None:
        if self.row_batch_rows <= 0:
            raise ValueError("row_batch_rows must be positive")
        if self.edge_batch_rows <= 0:
            raise ValueError("edge_batch_rows must be positive")
        if self.merge_fanout < 2:
            raise ValueError("merge_fanout must be at least 2")
        if self.key_history_buffer <= 0:
            raise ValueError("key_history_buffer must be positive")


@dataclass(frozen=True, slots=True)
class PitBoundedRecord:
    """One item of :meth:`PitSelector.iter_bounded`'s output stream.

    Exactly one record per evaluated ``PointInTimeSelection`` instant of a key, in key-then-
    instant order (the same order :func:`_evaluate` already produces per key). ``lineage`` /
    ``evidence_gap`` are attached the first time — within this key's release scope — their
    revision is selected, and are ``None`` on every other record (every ABSENT / CONFLICT
    instant, and every repeat SELECTED instant of an already-emitted revision); folding over the
    stream and keeping the non-``None`` ones reconstructs the same ``revision -> lineage / gap``
    mapping :attr:`PitSelection.lineage` / :attr:`PitSelection.evidence_gaps` hold for a key,
    without this generator ever retaining it for more than one key at a time.

    ``owner_event_time`` is the earliest event of the key's whole revision chain (as read by the
    key closure, :func:`_key_closure`'s ``earliest``): identical on every record of one key, it is
    the slice-ownership witness a caller folding this stream into one group per key
    (``PitKeyGroup.owner_event_time``) needs without a second read of the Canonical rows.
    ``event_time`` is the selected revision's own proven row time column (``event_time`` /
    ``interval_start``, exactly v2's row time): set whenever ``selection.status`` is
    ``SELECTED`` (on every such record, not just the first for a revision — unlike ``lineage`` /
    ``evidence_gap`` it is not "carried"; it is cheap to re-attach from the key's already-held
    rows), and ``None`` on every ABSENT / CONFLICT record.
    """

    observation_key: str
    selection: PointInTimeSelection
    lineage: SelectedRevisionLineage | None
    evidence_gap: EvidenceGap | None
    owner_event_time: datetime
    event_time: datetime | None


def _equals(column: str, value: object) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _at_least(column: str, value: object) -> BooleanExpression:
    return GreaterThanOrEqual(column, value)  # type: ignore[call-arg, arg-type]


def _below(column: str, value: object) -> BooleanExpression:
    return LessThan(column, value)  # type: ignore[call-arg, arg-type]


def _check_bindings(spec: PointInTimeSpec) -> None:
    if spec.point_in_time_binding != PIT_BINDING:
        raise PitSpecError(
            f"the spec's PIT rule {spec.point_in_time_binding.policy_id}@"
            f"{spec.point_in_time_binding.version} is not {PIT_RULE_ID}@{PIT_RULE_VERSION} "
            "with this hash"
        )
    for field, required in REQUIRED_BINDINGS.items():
        bound = {binding.policy_id: binding for binding in getattr(spec, field)}
        for binding in required:
            if bound.get(binding.policy_id) != binding:
                raise PitSpecError(
                    f"{field} must bind {binding.policy_id}@{binding.version} with its exact hash"
                )


class PitSelector:
    """Deterministic PIT selection at a spec's bound snapshots; never writes."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        canonical_scratch_directory: Path,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._canonical_scratch_directory = canonical_scratch_directory
        #: The last spec's bound snapshots, their view and immutable-view normalizer, and the
        #: verified Raw edges per (data type, symbol, day): bound snapshots never change, so a
        #: caller selecting many slices under one spec proves each unit and day once (G3-S3).
        self._bound: tuple[tuple[str, str], ...] | None = None
        self._view: PinnedCatalogView | None = None
        self._normalizer: CanonicalNormalizer | None = None
        self._edges: dict[tuple[str, str, date], tuple[ChannelEdge, ...]] = {}

    def _pinned(self, spec: PointInTimeSpec) -> PinnedCatalogView:
        bound = tuple(sorted(spec.snapshot_bindings.items()))
        if bound != self._bound or self._view is None:
            view = PinnedCatalogView(self._adapter, spec.snapshot_bindings)
            normalizer = CanonicalNormalizer(
                view,
                self._storage,
                scratch_directory=self._canonical_scratch_directory,
            )
            old_normalizer = self._normalizer
            self._bound = bound
            self._view = view
            self._normalizer = normalizer
            self._edges = {}
            if old_normalizer is not None:
                old_normalizer.close()
        return self._view

    def select(
        self,
        spec: PointInTimeSpec,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        touching: bool = False,
    ) -> PitSelection:
        """Select the keys the window owns (default) or, with ``touching``, every key with any
        revision in it (a reader that de-duplicates across windows, e.g. a quality report)."""
        if not isinstance(spec, PointInTimeSpec):
            raise PitSpecError("spec must be a PointInTimeSpec")
        _check_bindings(spec)
        canonical = rules.CANONICAL_TABLES.get(data_type)
        if canonical is None:
            raise PitSpecError(f"unsupported data_type {data_type!r}")
        instrument = rules.SYMBOLS.get(symbol)
        if instrument is None:
            raise PitSpecError(f"{symbol!r} is not a first-slice venue symbol")
        days = _days(start, end)
        if canonical.table not in spec.snapshot_bindings:
            raise PitSpecError(f"the spec does not bind {canonical.table}")
        view = self._pinned(spec)

        rows = self._canonical_rows(
            view, canonical.table, data_type, instrument.symbol, start, end, touching
        )
        verified = self._verify_canonical(view, rows)
        try:
            moved = effective_available_times(verified, bound=assumption_bound(spec))
        except AssumptionSpecError as exc:
            raise PitSpecError(str(exc)) from None
        available = {
            row["revision_id"]: moved[row["revision_id"]][1]
            if row["revision_id"] in moved
            else row["available_time"]
            for row in verified
        }
        by_key: dict[str, list[Mapping[str, Any]]] = {}
        for row in verified:
            by_key.setdefault(row["observation_key"], []).append(row)
        column = _time_column(data_type)
        # Edges of every day a revision of the window's keys lies in (the key closure below).
        days = sorted(set(days) | {row[column].astimezone(UTC).date() for row in verified})
        edges = self._mapped_edges(view, spec, data_type, symbol, days, by_key)

        selections: list[PointInTimeSelection] = []
        lineage: dict[str, SelectedRevisionLineage] = {}
        gaps: dict[str, EvidenceGap] = {}
        conflicts: set[str] = set()
        records_by_key: dict[str, tuple[RevisionRecord, ...]] = {}
        selected_rows: dict[str, Mapping[str, Any]] = {}
        for key in sorted(by_key):
            key_rows = {row["revision_id"]: row for row in by_key[key]}
            records = tuple(
                revision_record_from_row(key_rows[revision]) for revision in sorted(key_rows)
            )
            records_by_key[key] = records
            for selection in _evaluate(key, records, edges.get(key, ()), spec, available):
                selections.append(selection)
                if selection.status is PointInTimeStatus.CONFLICT:
                    conflicts.add(key)
                elif selection.status is PointInTimeStatus.SELECTED:
                    revision = selection.selected_revision_id
                    if revision is None:  # pragma: no cover - the contract forbids it
                        raise CatalogIntegrityError("a selected result without a revision")
                    row = key_rows[revision]
                    if revision in moved:
                        row = dict(row, available_time=moved[revision][1])
                    selected_rows[revision] = row
                    lineage[revision] = SelectedRevisionLineage(
                        canonical_table=canonical.table,
                        canonical_revision_id=revision,
                        raw_table=row["lineage_raw_table"],
                        raw_revision_id=row["lineage_raw_revision_id"],
                        source_table=row["lineage_source_table"],
                        source_revision_id=row["lineage_source_revision_id"],
                    )
                    if row["availability_evidence_gap"] is not None:
                        gaps[revision] = EvidenceGap(
                            canonical.table, revision, row["availability_evidence_gap"]
                        )
        return PitSelection(
            canonical_table=canonical.table,
            selections=tuple(selections),
            lineage=tuple(lineage[revision] for revision in sorted(lineage)),
            evidence_gaps=tuple(gaps[revision] for revision in sorted(gaps)),
            conflicts=tuple(sorted(conflicts)),
            records=records_by_key,
            edges={key: tuple(value) for key, value in edges.items()},
            evidence_bound=BINANCE_SPOT_PRECEDENCE_EVIDENCE.table in spec.snapshot_bindings,
            selected_rows={revision: selected_rows[revision] for revision in sorted(selected_rows)},
            assumed={revision: moved[revision] for revision in sorted(moved)},
        )

    # ------------------------------------------------------------------ reads and proofs

    def _canonical_rows(
        self,
        view: PinnedCatalogView,
        table: str,
        data_type: str,
        canonical_symbol: str,
        start: datetime,
        end: datetime,
        touching: bool,
    ) -> list[Mapping[str, Any]]:
        """Every revision of every key the window owns (key closure, G3-S3 review G-1 / H-1).

        Revisions of one key may disagree on their event time (a mismatching archive and REST
        copy of one trade), so a key's revisions within ``_KEY_REACH`` of the window are read and
        evaluated together, and the key belongs to the window holding its **earliest** such event
        — exactly one of any partition of time into windows, so slices never select a key twice.
        Revisions further apart than ``_KEY_REACH`` are not matched (a known limit: each side is
        then evaluated alone in its own window).
        """
        definition = rules.CANONICAL_TABLES[data_type]
        column = _time_column(data_type)
        keys = view.scan_columns(
            table,
            columns=("observation_key",),
            row_filter=And(
                _equals("symbol", canonical_symbol),
                And(_at_least(column, start), _below(column, end)),
            ),
        ).column("observation_key")
        wanted = set(keys.to_pylist())
        if not wanted:
            return []
        columns = tuple(field.name for field in definition.arrow_schema)
        members = pa.array(sorted(wanted), type=pa.string())

        def scan(names: Sequence[str], low: datetime, high: datetime) -> pa.Table:
            return view.scan_columns(
                table,
                columns=names,
                row_filter=And(
                    _equals("symbol", canonical_symbol),
                    And(_at_least(column, low), _below(column, high)),
                ),
            )

        def read(low: datetime, high: datetime) -> list[Mapping[str, Any]]:
            return _wanted_rows(scan, members, columns, column, low, high)

        return _key_closure(read, column, start, end, touching=touching)

    def _verify_canonical(
        self, view: PinnedCatalogView, rows: Sequence[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        """Every row read must be exactly a row its unit re-normalizes to at these snapshots."""
        normalizer = self._normalizer or CanonicalNormalizer(
            view,
            self._storage,
            scratch_directory=self._canonical_scratch_directory,
        )
        units: dict[tuple[str, str], set[int]] = {}
        for row in rows:
            unit = (row["lineage_raw_table"], row["lineage_source_revision_id"])
            units.setdefault(unit, set()).add(row["arrival_seq"])
        proven: dict[str, Mapping[str, Any]] = {}
        for (raw_table, source), seqs in sorted(units.items()):
            # Only the committed batches holding what was read are proven and kept (G3-S2);
            # the unit-wide facts are still checked by verify_unit.
            for row in normalizer.verify_unit(raw_table, source, arrival_seqs=seqs):
                proven[row["revision_id"]] = row
        for row in rows:
            expected = proven.get(row["revision_id"])
            if expected is None or any(row[name] != value for name, value in expected.items()):
                raise CatalogIntegrityError(
                    f"Canonical revision {row['revision_id']} is not what its unit normalizes to "
                    "at the bound snapshots"
                )
        seen: set[str] = set()
        for row in rows:
            if row["revision_id"] in seen:
                raise CatalogIntegrityError(
                    f"Canonical revision {row['revision_id']} is read twice"
                )
            seen.add(row["revision_id"])
        return list(rows)

    def _mapped_edges(
        self,
        view: PinnedCatalogView,
        spec: PointInTimeSpec,
        data_type: str,
        symbol: str,
        days: Iterable[date],
        by_key: Mapping[str, Iterable[Mapping[str, Any]]],
        *,
        cache_verified_edges: bool = True,
    ) -> dict[str, list[PrecedenceEvidence]]:
        evidence_snapshot = spec.snapshot_bindings.get(BINANCE_SPOT_PRECEDENCE_EVIDENCE.table)
        if evidence_snapshot is None:
            # Unbound = no edges. That can only turn a selection into a conflict (every
            # cross-channel edge needs an archive endpoint of the same key), never into another
            # selection, and it stays reproducible. An evidence table that has never been
            # written has no snapshot and cannot be bound at all; so ADR-0027 §13 is enforced
            # where the current state is known — the dataset builder (F3) must bind it whenever
            # it has a snapshot — and ``PitSelection.evidence_bound`` records which case this is.
            return {}
        reconciler = ChannelReconciler(view, self._storage)
        # A key's REST revisions may fall on several UTC days, and every such day's partition
        # returns the key's edges (D3E-R3): one Raw edge is mapped once, by its stable edge_id,
        # and every copy of it must be the same verified edge (anything else fails closed).
        unique: dict[str, ChannelEdge] = {}
        for day in days:
            edge_cache_key = (data_type, symbol, day)
            verified = self._edges.get(edge_cache_key) if cache_verified_edges else None
            if verified is None:
                verified = tuple(reconciler.verified_edges(data_type, symbol, day))
                if cache_verified_edges:
                    self._edges[edge_cache_key] = verified
            for edge in verified:
                seen = unique.setdefault(edge.edge_id, edge)
                if seen != edge or seen.row() != edge.row():
                    raise CatalogIntegrityError(
                        f"Raw edge {edge.edge_id} is verified with different content on "
                        "different days at the bound snapshots"
                    )
        mapped: dict[str, list[PrecedenceEvidence]] = {}
        for edge_id in sorted(unique):
            edge = unique[edge_id]
            raw = edge.evidence
            rows = by_key.get(raw.observation_key, ())
            ends = []
            for table, revision in (
                (edge.revision_table, raw.revision_id),
                (edge.superseded_table, raw.superseded_revision_id),
            ):
                found = [
                    row
                    for row in rows
                    if row["lineage_raw_table"] == table
                    and row["lineage_raw_revision_id"] == revision
                ]
                if len(found) > 1:
                    raise CatalogIntegrityError(
                        f"Raw revision {revision} has {len(found)} Canonical images"
                    )
                ends.append(found[0] if found else None)
            if ends[0] is None or ends[1] is None:
                continue  # an endpoint not normalized at these snapshots: no Canonical edge
            mapped.setdefault(raw.observation_key, []).append(
                rules.map_channel_edge(
                    raw,
                    edge.edge_id,
                    evidence_snapshot,
                    revision_record_from_row(ends[0]),
                    revision_record_from_row(ends[1]),
                )
            )
        return mapped

    # ------------------------------------------------------------ v3 fixed-working-set generator

    def iter_bounded(
        self,
        spec: PointInTimeSpec,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        params: PitRunParams,
        touching: bool = False,
    ) -> AbstractContextManager[Iterator[PitBoundedRecord]]:
        """v3 sorted-run selection entry point (ADR-0077 §6.1.2 / §6.1.3).

        Same bindings, pinned/proven reads, edge mapping, dual-cutoff candidate rule and
        competing-heads rule as :meth:`select` (this method calls the same private helpers, does
        not duplicate or alter them, and legacy ``select`` is byte-for-byte unaffected by this
        method's existence). What differs is the shape of the answer and how it is produced:

        - ``select`` groups the window's verified rows into a ``dict[str, list[...]]`` keyed by
          ``observation_key`` and every mapped edge into a second such dict, then accumulates
          ``lineage`` / ``evidence_gaps`` / ``selected_rows`` / ``conflicts`` across *every* key
          before returning one :class:`PitSelection` holding it all;
        - ``iter_bounded`` verifies fixed-size row batches, spills rows into content-addressed
          sorted runs, and consumes their root through fanout-bounded multi-pass merging. It
          processes one key at a time and separately spills / merges that key's mapped edges;
          it does not construct the whole-window ``by_key`` or ``edges`` mappings or retain a
          list of every run reference. Key ownership, availability, maximal-head selection and
          duplicate rejection retain the v2 semantics.

        **Current read boundary**: this path uses fixed Arrow batches and sorted runs for the
        candidate-key and Canonical-row scans. A key's source-row lookup, ownership minimum,
        day traversal, availability changes and availability lookup are run-backed; none keeps a
        second row dictionary, day set, change set, or availability map. The current graph
        authority still requires a `RevisionRecord` tuple and mapped-edge tuple, and
        `RevisionGraph` / `maximal_heads` build their own O(N) graph state. A conflict can also
        require the complete `maximal_heads` output tuple (see the separate decision packet).
        Those graph/output allocations remain ADR-0077 acceptance blockers. ADR-0077 §10 also
        requires a separate byte-level E1-CAP-1 measurement; no capacity pass is implied here.

        A conflict is reported inline (``selection.status is PointInTimeStatus.CONFLICT``) on the
        record itself, in place of ``select``'s separately collected ``conflicts`` tuple; a caller
        that must fail closed on any conflict (mirroring ``PitSelection.require_no_conflict``)
        checks each yielded record as it arrives.
        """
        return _pit_bounded_stream(
            self, spec, data_type, symbol, start, end, params=params, touching=touching
        )


def _wanted_rows(
    scan: Callable[[Sequence[str], datetime, datetime], pa.Table],
    members: pa.Array,
    columns: Sequence[str],
    column: str,
    low: datetime,
    high: datetime,
) -> list[Mapping[str, Any]]:
    """The rows in ``[low, high)`` whose ``observation_key`` is in ``members`` (G3-P).

    ``scan(names, a, b)`` reads ``names`` of the symbol's rows with ``a <= column < b`` at one
    pinned snapshot. The result holds exactly the rows one scan filtered by ``In(observation_key,
    members)`` over ``[low, high)`` returns, each once (the order may differ; nothing downstream
    depends on it). That single scan cannot prune data files by the key set (PyIceberg stops
    trying above 200 literals, and below it the keys' shared prefix defeats the 16-character
    string statistics) and binds / converts the whole set per data file (keys x files). Instead:

    1. **locate** — per UTC day (one partition) of the range, only ``(observation_key, column)``
       is read; every row is counted into its UTC hour, and the hours holding a member row
       (an Arrow ``is_in``: the membership test the scan filter makes) are noted;
    2. **fetch** — runs of consecutive noted hours, clipped to ``[low, high)`` and holding at most
       ``_FETCH_ROWS`` rows in all (a larger hour alone), are read in full and filtered by the same
       membership test.

    The runs are disjoint, lie in ``[low, high)`` and cover every hour holding a member row of
    this immutable snapshot, so every member row is returned exactly once. Memory is bounded by
    one day of two columns plus one run of full rows, never by the whole (widened) range.
    """
    counts: dict[datetime, int] = {}
    noted: set[datetime] = set()
    day = datetime.combine(low.astimezone(UTC).date(), time(), tzinfo=UTC)
    while day < high:
        piece = scan(("observation_key", column), max(low, day), min(high, day + _DAY))
        hours = pc.floor_temporal(piece.column(column), unit="hour")
        for entry in pc.value_counts(hours).to_pylist():
            counts[entry["values"].astimezone(UTC)] = entry["counts"]
        member = pc.is_in(piece.column("observation_key"), value_set=members)
        noted.update(hour.astimezone(UTC) for hour in pc.unique(hours.filter(member)).to_pylist())
        day += _DAY
    rows: list[Mapping[str, Any]] = []
    for first, last in _runs(sorted(noted), counts):
        piece = scan(columns, max(low, first), min(high, last + _HOUR))
        member = pc.is_in(piece.column("observation_key"), value_set=members)
        rows.extend(piece.filter(member).to_pylist())
    return rows


def _runs(
    hours: Sequence[datetime], counts: Mapping[datetime, int]
) -> list[tuple[datetime, datetime]]:
    """Ascending ``hours`` as runs of consecutive hours of at most ``_FETCH_ROWS`` rows each."""
    runs: list[tuple[datetime, datetime]] = []
    total = 0
    for hour in hours:
        if runs and hour == runs[-1][1] + _HOUR and total + counts[hour] <= _FETCH_ROWS:
            runs[-1] = (runs[-1][0], hour)
            total += counts[hour]
        else:
            runs.append((hour, hour))
            total = counts[hour]
    return runs


def _key_closure(
    read: Callable[[datetime, datetime], list[Mapping[str, Any]]],
    column: str,
    start: datetime,
    end: datetime,
    *,
    touching: bool,
) -> list[Mapping[str, Any]]:
    """The window's keys' chains, read until closed; owned keys only unless ``touching``.

    Every kept chain must lie wholly inside what was read, with a reach to spare on both sides;
    otherwise the read widens (review G3 cursor-1): a chain's earliest event — and so the one
    window owning the key — never depends on which window reads it.
    """
    low, high = start - _KEY_REACH, end + _KEY_REACH
    for _ in range(_CLOSURE_STEPS):
        rows = _chained(read(low, high), column, start, end)
        if not rows:
            return []
        times = [row[column] for row in rows]
        wider = (min(low, min(times) - _KEY_REACH), max(high, max(times) + _KEY_REACH))
        if wider == (low, high):
            break
        low, high = wider
    else:
        raise CatalogIntegrityError(
            f"key revisions around {start.isoformat()} chain beyond {_CLOSURE_STEPS} closure steps"
        )
    if touching:
        return rows
    earliest: dict[str, datetime] = {}
    for row in rows:
        key, at = row["observation_key"], row[column]
        if key not in earliest or at < earliest[key]:
            earliest[key] = at
    return [row for row in rows if start <= earliest[row["observation_key"]] < end]


def _chained(
    rows: Sequence[Mapping[str, Any]], column: str, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    """Per key, the chains (revisions linked by gaps <= ``_KEY_REACH``) touching the window."""
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(row["observation_key"], []).append(row)
    kept: list[Mapping[str, Any]] = []
    for members in by_key.values():
        members.sort(key=lambda row: (row[column], row["revision_id"]))
        chain: list[Mapping[str, Any]] = []
        for row in members:
            if chain and row[column] - chain[-1][column] > _KEY_REACH:
                if any(start <= item[column] < end for item in chain):
                    kept.extend(chain)
                chain = []
            chain.append(row)
        if any(start <= item[column] < end for item in chain):
            kept.extend(chain)
    return kept


def _time_column(data_type: str) -> str:
    return "event_time" if data_type == "agg_trades" else "interval_start"


def _days(start: datetime, end: datetime) -> list[date]:
    """The UTC days the window ``[start, end)`` touches (any UTC instants, G3-S2)."""
    for label, value in (("start", start), ("end", end)):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PitSpecError(f"{label} must be a UTC datetime")
    if not start < end:
        raise PitSpecError("the window must not be empty")
    days: list[date] = []
    day = start.astimezone(UTC).date()
    while datetime.combine(day, time(), tzinfo=UTC) < end:
        days.append(day)
        day += _DAY
    return days


def _heads(
    records: Sequence[RevisionRecord],
    edges: Sequence[PrecedenceEvidence],
    at: datetime,
    cutoff: datetime,
    available: Mapping[str, datetime],
) -> tuple[str, ...]:
    known = [item for item in records if item.availability.times.knowledge_time <= cutoff]
    known_edges = [item for item in edges if item.knowledge_time <= cutoff]
    try:
        RevisionGraph(revisions=tuple(known), precedence_evidence=tuple(known_edges))
    except ValueError as exc:
        raise CatalogIntegrityError(f"the Canonical revision graph is invalid: {exc}") from None
    candidates = [item for item in known if available[item.revision_id] <= at]
    if not candidates:
        return ()
    # ``maximal_heads`` walks every known edge from every candidate, through any known (even
    # not-yet-available) revision: the heads are the candidates no candidate reaches.
    return tuple(sorted(maximal_heads(candidates, known_edges)))


def _evaluate(
    key: str,
    records: Sequence[RevisionRecord],
    edges: Sequence[PrecedenceEvidence],
    spec: PointInTimeSpec,
    available: Mapping[str, datetime],
) -> list[PointInTimeSelection]:
    return list(
        _iter_evaluate_at_instants(
            key, _evaluation_instants(records, spec, available), records, edges, spec, available
        )
    )


def _evaluation_instants(
    records: Sequence[RevisionRecord],
    spec: PointInTimeSpec,
    available: Mapping[str, datetime],
) -> Iterator[datetime]:
    """Yield the exact v2 evaluation instants in their canonical order.

    Kept separate from evaluation so the bounded path can supply instants from a sorted run
    without first constructing the interval-sized ``changes`` set and ``instants`` list.
    """
    cutoff = spec.knowledge_cutoff
    if spec.simulation_time is not None:
        yield spec.simulation_time
        return
    start, end = spec.simulation_start, spec.simulation_end
    if start is None or end is None:  # pragma: no cover - the contract forbids it
        raise PitSpecError("the spec has neither a simulation time nor an interval")
    changes = {
        available[item.revision_id]
        for item in records
        if item.availability.times.knowledge_time <= cutoff
        and start < available[item.revision_id] < end
    }
    yield start
    yield from sorted(changes)


def _iter_evaluate_at_instants(
    key: str,
    instants: Iterator[datetime],
    records: Sequence[RevisionRecord],
    edges: Sequence[PrecedenceEvidence],
    spec: PointInTimeSpec,
    available: Mapping[str, datetime],
) -> Iterator[PointInTimeSelection]:
    """Evaluate ordered instants while retaining only the previous result.

    The v2 wrapper still returns its historical list. The bounded selector uses this iterator,
    allowing it to eventually source interval changes directly from a sorted run and expose
    results one at a time without an internal ``results`` list.
    """
    cutoff = spec.knowledge_cutoff
    previous: tuple[PointInTimeStatus, tuple[str, ...]] | None = None
    for at in instants:
        heads = _heads(records, edges, at, cutoff, available)
        status = (
            PointInTimeStatus.ABSENT
            if not heads
            else PointInTimeStatus.SELECTED
            if len(heads) == 1
            else PointInTimeStatus.CONFLICT
        )
        if previous == (status, heads):
            continue
        previous = (status, heads)
        yield PointInTimeSelection(
            observation_key=key,
            simulation_time=at,
            knowledge_cutoff=cutoff,
            status=status,
            selected_revision_id=heads[0] if status is PointInTimeStatus.SELECTED else None,
            maximal_heads=heads,
        )


# ============================================================================================
# v3 fixed-working-set generator (ADR-0077 §6.1.2 / §6.1.3): sort-then-merge implementation of
# PitSelector.iter_bounded. Reuses the same private read / proof / edge-mapping helpers as
# select() (_canonical_rows, _verify_canonical, _mapped_edges) unchanged; only the grouping and
# evaluation phase is rebuilt on infrastructure.pit.runs' content-addressed sorted runs.
# ============================================================================================


def _pit_row_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """Run order for verified rows: ``(observation_key, revision_id)`` (ADR-0077 §6.1.2)."""
    return (row["observation_key"], row["revision_id"])


def _pit_row_group_key(row: Mapping[str, Any]) -> str:
    return row["observation_key"]


class _RunBackedRows:
    """A re-readable single-key view whose rows stay in the sorted-run store.

    Returning a tuple here used to duplicate the complete key history just to map Raw endpoints.
    Each iteration now opens one bounded run reader and closes it deterministically.
    """

    def __init__(self, storage: StorageAdapter, root: RunRef) -> None:
        self._storage = storage
        self._root = root

    def __len__(self) -> int:
        return self._root.record_count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        with iter_run(self._storage, self._root) as rows:
            yield from rows

    def find(self, revision_id: str) -> Mapping[str, Any] | None:
        for row in self:
            if row["revision_id"] == revision_id:
                return row
        return None


def _bounded_mapped_edge_run(
    selector: PitSelector,
    view: PinnedCatalogView,
    spec: PointInTimeSpec,
    data_type: str,
    symbol: str,
    key: str,
    days: Iterable[date],
    rows: _RunBackedRows,
    *,
    params: PitRunParams,
) -> RunRef | None:
    """Verify, deduplicate and map one key's Raw edges without an O(E) Python map/list."""
    evidence_snapshot = spec.snapshot_bindings.get(BINANCE_SPOT_PRECEDENCE_EVIDENCE.table)
    if evidence_snapshot is None:
        return None
    storage = selector._storage
    limits = params.limits
    reconciler = ChannelReconciler(view, storage)

    with RunSetBuilder(
        storage,
        key=lambda item: item["edge_id"],
        capacity=params.edge_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    ) as raw_builder:
        for day in days:
            for edge in reconciler.verified_edges(data_type, symbol, day):
                raw_builder.add(edge.row())
        raw_root = raw_builder.finish()
    if raw_root is None:
        return None

    def endpoint(table: str, revision: str) -> list[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = []
        for row in rows:
            if row["lineage_raw_table"] == table and row["lineage_raw_revision_id"] == revision:
                found.append(row)
                if len(found) > 1:
                    break
        return found

    mapped_root: RunRef | None
    with RunSetBuilder(
        storage,
        key=_pit_edge_sort_key,
        capacity=params.edge_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    ) as mapped_builder:
        with iter_run(storage, raw_root) as raw_rows:
            for edge_id, same_id in itertools.groupby(raw_rows, key=lambda item: item["edge_id"]):
                first: Mapping[str, Any] | None = None
                inconsistent = False
                for item in same_id:
                    if first is None:
                        first = item
                    elif item != first:
                        inconsistent = True
                if first is None:  # pragma: no cover - groupby always yields at least one item
                    raise CatalogIntegrityError("a grouped Raw edge unexpectedly became empty")
                if inconsistent:
                    raise CatalogIntegrityError(
                        f"Raw edge {edge_id} is verified with different content on "
                        "different days at the bound snapshots"
                    )
                if first["observation_key"] != key:
                    continue
                archive = endpoint(first["revision_table"], first["revision_id"])
                rest = endpoint(first["superseded_table"], first["superseded_revision_id"])
                if len(archive) > 1 or len(rest) > 1:
                    duplicate = (
                        first["revision_id"]
                        if len(archive) > 1
                        else first["superseded_revision_id"]
                    )
                    count = len(archive) if len(archive) > 1 else len(rest)
                    raise CatalogIntegrityError(
                        f"Raw revision {duplicate} has {count} Canonical images"
                    )
                if not archive or not rest:
                    continue
                mapped = rules.map_channel_edge(
                    evidence_from_row(first),
                    edge_id,
                    evidence_snapshot,
                    revision_record_from_row(archive[0]),
                    revision_record_from_row(rest[0]),
                )
                mapped_builder.add(
                    {"observation_key": key, "evidence": mapped.model_dump(mode="json")}
                )
        mapped_root = mapped_builder.finish()
    return mapped_root


class _RunBackedAvailability(Mapping[str, datetime]):
    """Resolve one revision's effective time by rereading its key run, with O(1) state."""

    def __init__(self, rows: _RunBackedRows, *, bound: bool) -> None:
        self._rows = rows
        self._bound = bound

    def __getitem__(self, revision_id: str) -> datetime:
        row = self._rows.find(revision_id)
        if row is None:
            raise KeyError(revision_id)
        moved = effective_available_times((row,), bound=self._bound).get(revision_id)
        return moved[1] if moved is not None else row["available_time"]

    def __iter__(self) -> Iterator[str]:
        for row in self._rows:
            yield row["revision_id"]

    def __len__(self) -> int:
        return len(self._rows)


@contextmanager
def _bounded_evaluation_instants(
    storage: StorageAdapter,
    rows: _RunBackedRows,
    records: Sequence[RevisionRecord],
    spec: PointInTimeSpec,
    *,
    bound: bool,
    params: PitRunParams,
) -> Iterator[Iterator[datetime]]:
    """Externally sort known revisions' availability changes, retaining only one instant.

    Legacy ``_evaluation_instants`` builds an interval-sized set and sorted list. The bounded
    path writes the exact same eligible instants to a sorted run and coalesces adjacent duplicate
    timestamps during readback.
    """
    if spec.simulation_time is not None:
        yield iter((spec.simulation_time,))
        return
    start, end = spec.simulation_start, spec.simulation_end
    if start is None or end is None:  # pragma: no cover - contract rejects this shape
        raise PitSpecError("the spec has neither a simulation time nor an interval")

    def changes() -> Iterator[Mapping[str, Any]]:
        for row, record in zip(rows, records, strict=True):
            if record.availability.times.knowledge_time > spec.knowledge_cutoff:
                continue
            moved = effective_available_times((row,), bound=bound).get(row["revision_id"])
            available_at = moved[1] if moved is not None else row["available_time"]
            if start < available_at < end:
                yield {"available_time": available_at, "revision_id": row["revision_id"]}

    with RunSetBuilder(
        storage,
        key=lambda row: (row["available_time"], row["revision_id"]),
        capacity=params.row_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=params.limits,
    ) as builder:
        builder.extend(changes())
        root = builder.finish()

    def instants() -> Iterator[datetime]:
        yield start
        if root is None:
            return
        previous: datetime | None = None
        with iter_run(storage, root) as changes_run:
            for item in changes_run:
                at = item["available_time"]
                if at != previous:
                    yield at
                    previous = at

    yield instants()


def _pit_edge_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    # A stable tiebreaker among one key's edges: the edge's own canonical JSON (edges carry no
    # single natural-order field of their own at this layer).
    return (row["observation_key"], canonical_json(row["evidence"]))


def _iter_bounded_canonical_rows(
    view: PinnedCatalogView,
    *,
    table: str,
    data_type: str,
    canonical_symbol: str,
    start: datetime,
    end: datetime,
    touching: bool,
    storage: StorageAdapter,
    params: PitRunParams,
) -> Iterator[Mapping[str, Any]]:
    """Stream owned Canonical rows via bounded candidate, closure and row runs.

    Candidate observation keys and each key's time-ordered closure are externally sorted; no
    whole-window key set or row list is built. Closure grows by querying disjoint time shells,
    then connected chains that touch the requested window are projected into one globally sorted
    ``(observation_key, revision_id)`` run. Run roots, rather than one ref per spill, cross each
    helper boundary.
    """
    column = _time_column(data_type)
    definition = rules.CANONICAL_TABLES[data_type]
    limits = params.limits

    def scan_rows(
        columns: Sequence[str], row_filter: BooleanExpression
    ) -> Iterator[Mapping[str, Any]]:
        reader = view.scan_column_batches(table, columns=columns, row_filter=row_filter)
        try:
            for batch in reader:
                yield from batch.to_pylist()
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    candidate_filter = And(
        _equals("symbol", canonical_symbol),
        And(_at_least(column, start), _below(column, end)),
    )
    with RunSetBuilder(
        storage,
        key=lambda row: row["observation_key"],
        capacity=params.row_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    ) as candidate_builder:
        for row in scan_rows(("observation_key",), candidate_filter):
            candidate_builder.add({"observation_key": row["observation_key"]})
        candidate_root = candidate_builder.finish()
    if candidate_root is None:
        return

    def key_rows(key: str, low: datetime, high: datetime) -> Iterator[Mapping[str, Any]]:
        filt = And(
            _equals("symbol", canonical_symbol),
            And(
                EqualTo("observation_key", key),
                And(_at_least(column, low), _below(column, high)),
            ),
        )
        yield from scan_rows(tuple(field.name for field in definition.arrow_schema), filt)

    def build_interval(key: str, low: datetime, high: datetime) -> RunRef | None:
        with RunSetBuilder(
            storage,
            key=lambda row: (row[column], row["revision_id"]),
            capacity=params.row_batch_rows,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as builder:
            builder.extend(key_rows(key, low, high))
            return builder.finish()

    def merge_shells(
        key: str,
        previous: RunRef,
        low: datetime,
        high: datetime,
        old_low: datetime,
        old_high: datetime,
    ) -> RunRef:
        with RunSetBuilder(
            storage,
            key=lambda row: (row[column], row["revision_id"]),
            capacity=params.row_batch_rows,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as builder:
            with iter_run(storage, previous) as old_rows:
                builder.extend(old_rows)
            if low < old_low:
                builder.extend(key_rows(key, low, old_low))
            if old_high < high:
                builder.extend(key_rows(key, old_high, high))
            result = builder.finish()
        if result is None:  # previous is non-empty; this is a storage integrity failure.
            raise CatalogIntegrityError("a Canonical key closure unexpectedly became empty")
        return result

    def touched_bounds(root: RunRef, low: datetime, high: datetime) -> tuple[datetime, datetime]:
        widened_low, widened_high = low, high
        chain_first: datetime | None = None
        chain_last: datetime | None = None
        chain_touches = False

        def close_chain() -> None:
            nonlocal widened_low, widened_high, chain_first, chain_last, chain_touches
            if chain_first is not None and chain_last is not None and chain_touches:
                widened_low = min(widened_low, chain_first - _KEY_REACH)
                widened_high = max(widened_high, chain_last + _KEY_REACH)
            chain_first = chain_last = None
            chain_touches = False

        with iter_run(storage, root) as records:
            for row in records:
                at = row[column]
                if chain_last is not None and at - chain_last > _KEY_REACH:
                    close_chain()
                if chain_first is None:
                    chain_first = at
                chain_last = at
                chain_touches = chain_touches or start <= at < end
            close_chain()
        return widened_low, widened_high

    def iter_kept_chains(root: RunRef, key: str) -> Iterator[Mapping[str, Any]]:
        chain_builder: RunSetBuilder | None = None
        chain_first: datetime | None = None
        chain_last: datetime | None = None
        chain_touches = False

        def release_chain() -> Iterator[Mapping[str, Any]]:
            nonlocal chain_builder, chain_first, chain_last, chain_touches
            if chain_builder is None:
                return
            chain_root = chain_builder.finish()
            keep = chain_touches and (
                touching or (chain_first is not None and start <= chain_first < end)
            )
            chain_builder = None
            chain_first = chain_last = None
            chain_touches = False
            if keep and chain_root is not None:
                with iter_run(storage, chain_root) as chain_rows:
                    yield from chain_rows

        try:
            with iter_run(storage, root) as records:
                for row in records:
                    at = row[column]
                    if chain_last is not None and at - chain_last > _KEY_REACH:
                        yield from release_chain()
                    if chain_builder is None:
                        chain_builder = RunSetBuilder(
                            storage,
                            key=lambda item: (item[column], item["revision_id"]),
                            capacity=params.row_batch_rows,
                            merge_fanout=params.merge_fanout,
                            limits=limits,
                        )
                        chain_first = at
                    chain_builder.add(row)
                    chain_last = at
                    chain_touches = chain_touches or start <= at < end
                yield from release_chain()
        finally:
            if chain_builder is not None:
                chain_builder.close()

    row_builder = RunSetBuilder(
        storage,
        key=_pit_row_sort_key,
        capacity=params.row_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    )
    try:
        with iter_run(storage, candidate_root) as candidates:
            last_key: str | None = None
            for candidate in candidates:
                key = candidate["observation_key"]
                if not isinstance(key, str) or not key:
                    raise CatalogIntegrityError("a Canonical observation_key is not a string")
                if key == last_key:
                    continue
                last_key = key
                low, high = start - _KEY_REACH, end + _KEY_REACH
                root = build_interval(key, low, high)
                if root is None:
                    raise CatalogIntegrityError(
                        f"a selected Canonical observation_key {key} has no revisions"
                    )
                for _ in range(_CLOSURE_STEPS):
                    next_low, next_high = touched_bounds(root, low, high)
                    if (next_low, next_high) == (low, high):
                        break
                    root = merge_shells(key, root, next_low, next_high, low, high)
                    low, high = next_low, next_high
                else:
                    raise CatalogIntegrityError(
                        f"key revisions around {start.isoformat()} chain beyond "
                        f"{_CLOSURE_STEPS} closure steps"
                    )
                row_builder.extend(iter_kept_chains(root, key))
        final_root = row_builder.finish()
        if final_root is not None:
            with iter_run(storage, final_root) as result_rows:
                yield from result_rows
    finally:
        # The builder itself owns no open reader; this drops bounded in-memory tail/ref state if
        # the output consumer closes before all candidate keys have been processed.
        row_builder.close()


@contextmanager
def _pit_bounded_stream(
    selector: PitSelector,
    spec: PointInTimeSpec,
    data_type: str,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    params: PitRunParams,
    touching: bool,
) -> Iterator[Iterator[PitBoundedRecord]]:
    if not isinstance(spec, PointInTimeSpec):
        raise PitSpecError("spec must be a PointInTimeSpec")
    _check_bindings(spec)
    canonical = rules.CANONICAL_TABLES.get(data_type)
    if canonical is None:
        raise PitSpecError(f"unsupported data_type {data_type!r}")
    instrument = rules.SYMBOLS.get(symbol)
    if instrument is None:
        raise PitSpecError(f"{symbol!r} is not a first-slice venue symbol")
    # Validate the window without retaining one ``date`` per calendar day. The bounded path
    # discovers only days carrying rows for the current key as it streams the sorted run.
    for label, value in (("start", start), ("end", end)):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PitSpecError(f"{label} must be a UTC datetime")
    if not start < end:
        raise PitSpecError("the window must not be empty")
    if canonical.table not in spec.snapshot_bindings:
        raise PitSpecError(f"the spec does not bind {canonical.table}")
    view = selector._pinned(spec)

    def _verified_in_batches() -> Iterator[Mapping[str, Any]]:
        """Verify bounded batches so the v3 path never builds a window-sized verified list."""
        batch: list[Mapping[str, Any]] = []
        source = _iter_bounded_canonical_rows(
            view,
            table=canonical.table,
            data_type=data_type,
            canonical_symbol=instrument.symbol,
            start=start,
            end=end,
            touching=touching,
            storage=selector._storage,
            params=params,
        )
        try:
            for row in source:
                batch.append(row)
                if len(batch) == params.row_batch_rows:
                    yield from selector._verify_canonical(view, batch)
                    batch.clear()
            if batch:
                yield from selector._verify_canonical(view, batch)
        finally:
            source.close()

    try:
        bound_assumption = assumption_bound(spec)
    except AssumptionSpecError as exc:
        raise PitSpecError(str(exc)) from None

    column = _time_column(data_type)
    storage = selector._storage
    limits = params.limits

    def _generate() -> Iterator[PitBoundedRecord]:
        with RunSetBuilder(
            storage,
            key=_pit_row_sort_key,
            capacity=params.row_batch_rows,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as row_builder:
            row_builder.extend(_verified_in_batches())
            row_root = row_builder.finish()
        if row_root is None:
            return
        with iter_run(storage, row_root) as merged_rows:
            last_identity: tuple[str, str] | None = None
            for row_key, row_group in itertools.groupby(merged_rows, key=_pit_row_group_key):
                with RunSetBuilder(
                    storage,
                    key=lambda item: item["revision_id"],
                    capacity=params.key_history_buffer,
                    merge_fanout=params.merge_fanout,
                    limits=limits,
                ) as key_builder:
                    for row in row_group:
                        identity = (row["observation_key"], row["revision_id"])
                        if identity == last_identity:
                            raise CatalogIntegrityError(
                                f"Canonical revision {identity[1]} is read twice"
                            )
                        last_identity = identity
                        key_builder.add(row)
                    key_root = key_builder.finish()
                if key_root is None:  # groupby yielded at least one row
                    raise CatalogIntegrityError("a grouped Canonical key unexpectedly became empty")
                with iter_run(storage, key_root) as key_row_iter:
                    records = tuple(revision_record_from_row(row) for row in key_row_iter)
                key_rows = _RunBackedRows(storage, key_root)
                # The key's whole read closure — not just the window's own instants — so this
                # is exactly the ``earliest`` value _key_closure filtered ownership on. Keep
                # only its scalar minimum instead of a second revision_id -> row dictionary.
                owner_at = min(row[column] for row in key_rows)

                first_day = min(row[column].astimezone(UTC).date() for row in key_rows)
                last_day = max(row[column].astimezone(UTC).date() for row in key_rows)

                def days_between(low: date = first_day, high: date = last_day) -> Iterator[date]:
                    day = low
                    while day <= high:
                        yield day
                        day += _DAY

                edge_root = _bounded_mapped_edge_run(
                    selector,
                    view,
                    spec,
                    data_type,
                    symbol,
                    row_key,
                    days_between(),
                    key_rows,
                    params=params,
                )
                key_edges: tuple[PrecedenceEvidence, ...]
                if edge_root is None:
                    key_edges = ()
                else:
                    with iter_run(storage, edge_root) as merged_edges:
                        parsed_edges: list[PrecedenceEvidence] = []
                        for item in merged_edges:
                            if item["observation_key"] != row_key:
                                raise CatalogIntegrityError(
                                    "a mapped edge references an observation_key with no "
                                    "corresponding Canonical rows in this window"
                                )
                            parsed_edges.append(PrecedenceEvidence.model_validate(item["evidence"]))
                        key_edges = tuple(parsed_edges)

                available = _RunBackedAvailability(key_rows, bound=bound_assumption)

                with _bounded_evaluation_instants(
                    storage,
                    key_rows,
                    records,
                    spec,
                    bound=bound_assumption,
                    params=params,
                ) as instants:
                    for selection in _iter_evaluate_at_instants(
                        row_key, instants, records, key_edges, spec, available
                    ):
                        lineage_out: SelectedRevisionLineage | None = None
                        gap_out: EvidenceGap | None = None
                        event_at: datetime | None = None
                        if selection.status is PointInTimeStatus.SELECTED:
                            revision = selection.selected_revision_id
                            if revision is None:  # pragma: no cover - the contract forbids it
                                raise CatalogIntegrityError("a selected result without a revision")
                            source_row = key_rows.find(revision)
                            if source_row is None:
                                raise CatalogIntegrityError(
                                    f"selected Canonical revision {revision} is absent from "
                                    "its key run"
                                )
                            # Eligible candidates only grow as simulation time advances. Once a
                            # revision stops being the unique head, it cannot become a head again;
                            # identical adjacent selections were already merged by the iterator.
                            # Therefore one scalar status replaces the O(N) seen_revisions set.
                            event_at = source_row[column]
                            moved = effective_available_times(
                                (source_row,), bound=bound_assumption
                            ).get(revision)
                            if moved is not None:
                                source_row = dict(source_row, available_time=moved[1])
                            lineage_out = SelectedRevisionLineage(
                                canonical_table=canonical.table,
                                canonical_revision_id=revision,
                                raw_table=source_row["lineage_raw_table"],
                                raw_revision_id=source_row["lineage_raw_revision_id"],
                                source_table=source_row["lineage_source_table"],
                                source_revision_id=source_row["lineage_source_revision_id"],
                            )
                            if source_row["availability_evidence_gap"] is not None:
                                gap_out = EvidenceGap(
                                    canonical.table,
                                    revision,
                                    source_row["availability_evidence_gap"],
                                )
                        yield PitBoundedRecord(
                            observation_key=row_key,
                            selection=selection,
                            lineage=lineage_out,
                            evidence_gap=gap_out,
                            owner_event_time=owner_at,
                            event_time=event_at,
                        )
                # records / key_edges remain the current graph semantics' materialized inputs;
                # row and availability maps are run-backed and go out of scope before the next
                # observation_key's group is even read off merged_rows.

    # ``_generate`` holds the merged row-root reader and, while processing one key, the
    # key / edge-root readers. They are closed explicitly on every exit of the
    # caller's ``with`` block -- full iteration, an early ``break``, or an exception -- rather
    # than left to whenever the generator object is garbage collected.
    # ``.close()`` throws ``GeneratorExit`` in at the generator's current (or not yet started)
    # suspension point, which the ``with`` statements above unwind exactly as any other exit.
    generated = _generate()
    try:
        yield generated
    finally:
        generated.close()
