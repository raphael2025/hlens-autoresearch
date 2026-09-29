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
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, cast

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
    KeyHistoryBuffer,
    RunLimits,
    RunRef,
    RunSetBuilder,
    iter_run,
)
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING, ChannelEdge
from infrastructure.revision.channel_reconcile import (
    ChannelReconciler,
    VerifiedEdgeRunParams,
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
    - ``key_history_buffer``: how many of one observation key's own rows
      :class:`infrastructure.pit.runs.KeyHistoryBuffer` holds before spilling the rest of that
      key's (unusually long) chain into its own run;
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

    def __init__(self, adapter: RevisionCatalog, storage: StorageAdapter) -> None:
        self._adapter = adapter
        self._storage = storage
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
            self._bound = bound
            self._view = PinnedCatalogView(self._adapter, spec.snapshot_bindings)
            self._normalizer = CanonicalNormalizer(self._view, self._storage)
            self._edges = {}
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
        normalizer = self._normalizer or CanonicalNormalizer(view, self._storage)
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
        by_key: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        only_key: str | None = None,
        edge_run_params: VerifiedEdgeRunParams | None = None,
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

            def consume(verified: Iterable[ChannelEdge]) -> None:
                for edge in verified:
                    if only_key is not None and edge.evidence.observation_key != only_key:
                        continue
                    seen = unique.setdefault(edge.edge_id, edge)
                    if seen != edge or seen.row() != edge.row():
                        raise CatalogIntegrityError(
                            f"Raw edge {edge.edge_id} is verified with different content on "
                            "different days at the bound snapshots"
                        )

            if edge_run_params is not None:
                with reconciler.iter_verified_edges(
                    data_type, symbol, day, params=edge_run_params
                ) as verified:
                    consume(verified)
                continue

            cache_key = (data_type, symbol, day)
            cached_verified: tuple[ChannelEdge, ...] | None = (
                self._edges.pop(cache_key, None)
                if only_key is not None
                else self._edges.get(cache_key)
            )
            if cached_verified is None:
                cached_verified = tuple(reconciler.verified_edges(data_type, symbol, day))
                # The bounded generator must not turn its streamed day cursor into an
                # all-window edge cache. Legacy select() keeps its reuse cache unchanged.
                if only_key is None:
                    self._edges[(data_type, symbol, day)] = cached_verified
            consume(cached_verified)
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
        """v3 fixed-working-set entry point (ADR-0077 §6.1.2 / §6.1.3).

        Same bindings, pinned/proven reads, edge mapping, dual-cutoff candidate rule and
        competing-heads rule as :meth:`select`; the legacy ``select`` path is unchanged. This
        path externally sorts wanted keys, closure rows and event days, then verifies and maps
        one observation key at a time before writing sorted row/edge runs:

        - ``select`` groups the window's verified rows into a ``dict[str, list[...]]`` keyed by
          ``observation_key`` and every mapped edge into a second such dict, then accumulates
          ``lineage`` / ``evidence_gaps`` / ``selected_rows`` / ``conflicts`` across *every* key
          before returning one :class:`PitSelection` holding it all;
        - ``iter_bounded`` spills wanted keys, each closure scan, and selected key chains through
          :class:`infrastructure.pit.runs.RunSetBuilder`; only the current key and chain writer
          are live. It then verifies each key and maps matching edges into final sorted runs.
          Each completed batch is folded into a fanout-bounded hierarchy, leaving one root per
          stream rather than a list of all run references. It reads those roots in key order
          with explicitly closed readers per stream. One key's rows are gathered (via
          :class:`infrastructure.pit.runs.KeyHistoryBuffer`, which itself spills to a run if that
          one key's own history exceeds ``params.key_history_buffer``), evaluated exactly as
          ``select`` would (:func:`_evaluate` / :func:`_heads`, unchanged), yielded as ordered
          :class:`PitBoundedRecord`, and released — the next key's key/records/lookup state does
          not coexist with this one's, and nothing about the *whole window's* result set is ever
          held in memory at once, addressing ADR-0077 §6.1.4's ban on ``by_key`` /
          ``records_by_key`` / ``selected_rows`` persisting for a whole ``select`` call.

        **Honest boundary**: canonical proof holds one observation key's rows, and evaluation
        retains that key's graph/history/head result under the existing output contract.
        ``ChannelReconciler.verified_edges()`` still returns a tuple produced from a full-day
        ``_plan``; edge working memory is therefore bounded by one day partition, not by a fixed
        byte or row cap. The sorted staging itself no longer grows with the number of window
        rows, keys, days, or disconnected chains. PyArrow adapter batch sizing and physical
        row-group/stripe byte caps remain separate infrastructure bounds and are not established
        by this PIT slice.

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
    _validate_window(start, end)
    days: list[date] = []
    day = start.astimezone(UTC).date()
    while datetime.combine(day, time(), tzinfo=UTC) < end:
        days.append(day)
        day += _DAY
    return days


def _validate_window(start: datetime, end: datetime) -> None:
    """Validate UTC half-open window bounds without enumerating the window's UTC days."""
    for label, value in (("start", start), ("end", end)):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PitSpecError(f"{label} must be a UTC datetime")
    if not start < end:
        raise PitSpecError("the window must not be empty")


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
) -> Iterator[PointInTimeSelection]:
    cutoff = spec.knowledge_cutoff
    if spec.simulation_time is not None:
        instants: Iterator[datetime] = iter((spec.simulation_time,))
    else:
        start, end = spec.simulation_start, spec.simulation_end
        if start is None or end is None:  # pragma: no cover - the contract forbids it
            raise PitSpecError("the spec has neither a simulation time nor an interval")
        # The ordered timeline is needed to preserve PIT semantics, but the extra set of the
        # same timestamps and a second result list are not. Sorting one generator keeps the
        # unavoidable time ordering in one collection; results are yielded immediately below.
        sorted_changes = sorted(
            available[item.revision_id]
            for item in records
            if item.availability.times.knowledge_time <= cutoff
            and start < available[item.revision_id] < end
        )
        unique_changes = (at for at, _ in itertools.groupby(sorted_changes))
        instants = itertools.chain((start,), unique_changes)
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
    return cast(str, row["observation_key"])


def _flatten_edges(
    edges: Mapping[str, Sequence[PrecedenceEvidence]],
) -> Iterator[dict[str, Any]]:
    """One run-row per mapped edge: ``{"observation_key", "evidence"}`` (``evidence`` is the
    edge's JSON-safe ``model_dump``; :class:`PrecedenceEvidence` is not itself a plain
    ``Mapping``, so it cannot be spilled directly through :mod:`infrastructure.pit.runs`'s row
    codec)."""
    for key, items in edges.items():
        for item in items:
            yield {"observation_key": key, "evidence": item.model_dump(mode="json")}


@contextmanager
def _mapped_edge_run_stream(
    selector: PitSelector,
    view: PinnedCatalogView,
    spec: PointInTimeSpec,
    data_type: str,
    symbol: str,
    days: Iterable[date],
    by_key: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    observation_key: str,
    params: PitRunParams,
    limits: RunLimits,
) -> Iterator[Iterator[dict[str, Any]]]:
    """Map one key's verified Raw edges through a sorted run, with no edge-sized dict/list.

    The day reconciler validates before yielding, and this staging additionally folds repeated
    copies of the same edge across owner days by adjacent ``edge_id`` comparison. The key's
    canonical rows remain the existing per-key graph boundary.
    """
    evidence_snapshot = spec.snapshot_bindings.get(BINANCE_SPOT_PRECEDENCE_EVIDENCE.table)
    if evidence_snapshot is None:
        yield iter(())
        return
    edge_params = VerifiedEdgeRunParams(
        row_capacity=params.edge_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    )
    reconciler = ChannelReconciler(view, selector._storage)
    with RunSetBuilder(
        selector._storage,
        key=lambda row: row["edge_id"],
        capacity=params.edge_batch_rows,
        merge_fanout=params.merge_fanout,
        limits=limits,
    ) as raw_edge_builder:
        for day in days:
            with reconciler.iter_verified_edges(
                data_type, symbol, day, params=edge_params
            ) as verified:
                for edge in verified:
                    if edge.evidence.observation_key == observation_key:
                        raw_edge_builder.add(edge.row())
        raw_root = raw_edge_builder.finish()

    @contextmanager
    def mapped_rows() -> Iterator[Iterator[dict[str, Any]]]:
        if raw_root is None:
            yield iter(())
            return
        with _root_rows(selector._storage, raw_root) as raw_rows:

            def mapped() -> Iterator[dict[str, Any]]:
                previous_id: str | None = None
                previous_row: Mapping[str, Any] | None = None
                rows = by_key.get(observation_key, ())
                for row in raw_rows:
                    edge_id = cast(str, row["edge_id"])
                    if edge_id == previous_id:
                        if row != previous_row:
                            raise CatalogIntegrityError(
                                f"Raw edge {edge_id} is verified with different content "
                                "on different days at the bound snapshots"
                            )
                        continue
                    previous_id, previous_row = edge_id, row
                    raw = evidence_from_row(row)
                    endpoints: list[Mapping[str, Any] | None] = []
                    for table, revision in (
                        (row["revision_table"], raw.revision_id),
                        (row["superseded_table"], raw.superseded_revision_id),
                    ):
                        found: Mapping[str, Any] | None = None
                        for canonical_row in rows:
                            if (
                                canonical_row["lineage_raw_table"] == table
                                and canonical_row["lineage_raw_revision_id"] == revision
                            ):
                                if found is not None:
                                    raise CatalogIntegrityError(
                                        f"Raw revision {revision} has multiple Canonical images"
                                    )
                                found = canonical_row
                        endpoints.append(found)
                    if endpoints[0] is None or endpoints[1] is None:
                        continue
                    evidence = rules.map_channel_edge(
                        raw,
                        edge_id,
                        evidence_snapshot,
                        revision_record_from_row(endpoints[0]),
                        revision_record_from_row(endpoints[1]),
                    )
                    yield {
                        "observation_key": observation_key,
                        "evidence": evidence.model_dump(mode="json"),
                    }

            yield mapped()

    with mapped_rows() as stream:
        yield stream


def _pit_edge_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    # A stable tiebreaker among one key's edges: the edge's own canonical JSON (edges carry no
    # single natural-order field of their own at this layer).
    return (row["observation_key"], canonical_json(row["evidence"]))


def _pit_edge_group_key(row: Mapping[str, Any]) -> str:
    return cast(str, row["observation_key"])


@contextmanager
def _root_rows(
    storage: StorageAdapter, root: RunRef | None
) -> Iterator[Iterator[Mapping[str, Any]]]:
    if root is None:
        with nullcontext(iter(())) as empty:
            yield empty
    else:
        with iter_run(storage, root) as records:
            yield records


@contextmanager
def _scan_batches(
    view: PinnedCatalogView,
    table: str,
    *,
    columns: Sequence[str],
    row_filter: BooleanExpression,
) -> Iterator[Iterator[pa.RecordBatch]]:
    """Yield one adapter batch at a time and close its reader on every exit path."""
    reader = view.scan_column_batches(table, columns=columns, row_filter=row_filter)
    try:
        yield iter(reader)
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


def _pit_canonical_row_roots(
    selector: PitSelector,
    view: PinnedCatalogView,
    table: str,
    data_type: str,
    canonical_symbol: str,
    start: datetime,
    end: datetime,
    *,
    params: PitRunParams,
    touching: bool,
) -> tuple[RunRef | None, RunRef | None]:
    """Return key-sorted closure rows and their distinct UTC event days through bounded runs.

    This is the v3 counterpart of ``_canonical_rows``. It does not create the window's wanted-key
    set or row list: wanted keys, each closure scan, and selected chains are external sorted runs.
    One key and one run-writer buffer are live at a time. The legacy v2 helper remains unchanged.
    """
    definition = rules.CANONICAL_TABLES[data_type]
    column = _time_column(data_type)
    storage, limits = selector._storage, params.limits
    capacity = params.row_batch_rows

    with RunSetBuilder(
        storage,
        key=lambda row: (row["observation_key"],),
        capacity=capacity,
        merge_fanout=params.merge_fanout,
        limits=limits,
    ) as target_builder:
        target_filter = And(
            _equals("symbol", canonical_symbol),
            And(_at_least(column, start), _below(column, end)),
        )
        with _scan_batches(
            view, table, columns=("observation_key",), row_filter=target_filter
        ) as batches:
            for batch in batches:
                for key in batch.column(0).to_pylist():
                    target_builder.add({"observation_key": key})
        target_root = target_builder.finish()

    if target_root is None:
        return None, None

    low, high = start - _KEY_REACH, end + _KEY_REACH
    for _ in range(_CLOSURE_STEPS):
        scan_filter = And(
            _equals("symbol", canonical_symbol),
            And(_at_least(column, low), _below(column, high)),
        )
        with RunSetBuilder(
            storage,
            key=lambda row: (row["observation_key"], row[column], row["revision_id"]),
            capacity=capacity,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as scan_builder:
            with _scan_batches(
                view,
                table,
                columns=tuple(field.name for field in definition.arrow_schema),
                row_filter=scan_filter,
            ) as batches:
                for batch in batches:
                    scan_builder.extend(batch.to_pylist())
            scan_root = scan_builder.finish()

        row_builder = RunSetBuilder(
            storage,
            key=_pit_row_sort_key,
            capacity=capacity,
            merge_fanout=params.merge_fanout,
            limits=limits,
        )
        extent_low: datetime | None = None
        extent_high: datetime | None = None
        with row_builder:
            with (
                _root_rows(storage, target_root) as target_rows,
                _root_rows(storage, scan_root) as scanned_rows,
            ):
                target_keys = (
                    key for key, _ in itertools.groupby(target_rows, key=_pit_row_group_key)
                )
                wanted = next(target_keys, None)
                for key, group in itertools.groupby(scanned_rows, key=_pit_row_group_key):
                    while wanted is not None and wanted < key:
                        wanted = next(target_keys, None)
                    if wanted != key:
                        continue

                    key_rows_builder = RunSetBuilder(
                        storage,
                        key=_pit_row_sort_key,
                        capacity=capacity,
                        merge_fanout=params.merge_fanout,
                        limits=limits,
                    )
                    key_low: datetime | None = None
                    key_high: datetime | None = None
                    chain_low: datetime | None = None
                    chain_high: datetime | None = None
                    previous_at: datetime | None = None
                    chain_builder: RunSetBuilder | None = None
                    chain_touches = False

                    def finish_chain(
                        key_rows_builder: RunSetBuilder = key_rows_builder,
                    ) -> None:
                        nonlocal \
                            chain_builder, \
                            chain_touches, \
                            chain_low, \
                            chain_high, \
                            key_low, \
                            key_high
                        if chain_builder is None:
                            return
                        chain_root = chain_builder.finish()
                        if chain_root is not None and chain_touches:
                            if chain_low is not None:
                                key_low = chain_low if key_low is None else min(key_low, chain_low)
                            if chain_high is not None:
                                key_high = (
                                    chain_high if key_high is None else max(key_high, chain_high)
                                )
                            with _root_rows(storage, chain_root) as chain_rows:
                                key_rows_builder.extend(chain_rows)
                        chain_builder = None
                        chain_touches = False
                        chain_low = None
                        chain_high = None

                    with key_rows_builder:
                        try:
                            for row in group:
                                at = row[column]
                                if previous_at is not None and at - previous_at > _KEY_REACH:
                                    finish_chain()
                                if chain_builder is None:
                                    chain_builder = RunSetBuilder(
                                        storage,
                                        key=_pit_row_sort_key,
                                        capacity=capacity,
                                        merge_fanout=params.merge_fanout,
                                        limits=limits,
                                    )
                                chain_builder.add(row)
                                chain_low = at if chain_low is None else min(chain_low, at)
                                chain_high = at if chain_high is None else max(chain_high, at)
                                if start <= at < end:
                                    chain_touches = True
                                previous_at = at
                            finish_chain()
                            key_root = key_rows_builder.finish()
                        finally:
                            # Only the current chain writer is live. Keeping finished context
                            # callbacks in an ExitStack made memory grow with the number of
                            # disconnected chains in a high-history observation key.
                            if chain_builder is not None:
                                chain_builder.close()
                                chain_builder = None

                    if key_root is not None and key_low is not None and key_high is not None:
                        # Ownership is based on the earliest event across all chains touching
                        # the requested window, exactly as _key_closure computes it.
                        extent_low = key_low if extent_low is None else min(extent_low, key_low)
                        extent_high = (
                            key_high if extent_high is None else max(extent_high, key_high)
                        )
                        if touching or start <= key_low < end:
                            with _root_rows(storage, key_root) as key_rows:
                                row_builder.extend(key_rows)

            selected_root = row_builder.finish()

        if extent_low is None or extent_high is None:
            return None, None
        wider = (min(low, extent_low - _KEY_REACH), max(high, extent_high + _KEY_REACH))
        if wider == (low, high):
            with RunSetBuilder(
                storage,
                key=lambda row: (row["day"],),
                capacity=capacity,
                merge_fanout=params.merge_fanout,
                limits=limits,
            ) as day_builder:
                cursor = datetime.combine(start.astimezone(UTC).date(), time(), tzinfo=UTC)
                while cursor < end:
                    day_builder.add({"day": cursor.date().isoformat()})
                    cursor += _DAY
                with _root_rows(storage, selected_root) as selected_rows:
                    for row in selected_rows:
                        day_builder.add({"day": row[column].astimezone(UTC).date().isoformat()})
                raw_day_root = day_builder.finish()
            with RunSetBuilder(
                storage,
                key=lambda row: (row["day"],),
                capacity=capacity,
                merge_fanout=params.merge_fanout,
                limits=limits,
            ) as unique_days:
                with _root_rows(storage, raw_day_root) as raw_days:
                    for _, group in itertools.groupby(raw_days, key=lambda row: row["day"]):
                        unique_days.add(next(group))
                day_root = unique_days.finish()
            return selected_root, day_root
        low, high = wider

    raise CatalogIntegrityError(
        f"key revisions around {start.isoformat()} chain beyond {_CLOSURE_STEPS} closure steps"
    )


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
    _validate_window(start, end)
    if canonical.table not in spec.snapshot_bindings:
        raise PitSpecError(f"the spec does not bind {canonical.table}")
    view = selector._pinned(spec)
    # The bounded path never uses the legacy all-day edge cache. Drop entries that a prior
    # select() call on this selector may have retained before starting this stream.
    selector._edges.clear()
    try:
        bound_assumption = assumption_bound(spec)
    except AssumptionSpecError as exc:
        raise PitSpecError(str(exc)) from None
    column = _time_column(data_type)
    storage = selector._storage
    limits = params.limits
    canonical_rows_root, day_root = _pit_canonical_row_roots(
        selector,
        view,
        canonical.table,
        data_type,
        instrument.symbol,
        start,
        end,
        params=params,
        touching=touching,
    )
    with (
        RunSetBuilder(
            storage,
            key=_pit_row_sort_key,
            capacity=params.row_batch_rows,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as row_run_set,
        RunSetBuilder(
            storage,
            key=_pit_edge_sort_key,
            capacity=params.edge_batch_rows,
            merge_fanout=params.merge_fanout,
            limits=limits,
        ) as edge_run_set,
    ):
        with _root_rows(storage, canonical_rows_root) as sorted_rows:
            for observation_key, key_group in itertools.groupby(
                sorted_rows, key=_pit_row_group_key
            ):
                # Canonical proof and edge endpoint resolution are key-local. The legacy
                # _verify_canonical contract accepts a sequence for one observation key; this
                # is the remaining per-key working set, never a whole-window collection.
                key_rows = list(key_group)
                verified_key = selector._verify_canonical(view, key_rows)
                by_key = {observation_key: verified_key}
                with _root_rows(storage, day_root) as day_rows:
                    edge_days = (
                        date.fromisoformat(cast(str, day_row["day"])) for day_row in day_rows
                    )
                    with _mapped_edge_run_stream(
                        selector,
                        view,
                        spec,
                        data_type,
                        symbol,
                        edge_days,
                        by_key,
                        observation_key=observation_key,
                        params=params,
                        limits=limits,
                    ) as mapped_edges:
                        row_run_set.extend(verified_key)
                        edge_run_set.extend(mapped_edges)
        row_root = row_run_set.finish()
        edge_root = edge_run_set.finish()

    def _generate() -> Generator[PitBoundedRecord]:
        with (
            _root_rows(storage, row_root) as merged_rows,
            _root_rows(storage, edge_root) as merged_edges,
        ):
            edge_iter = iter(itertools.groupby(merged_edges, key=_pit_edge_group_key))
            pending_edge_key, pending_edge_group = next(edge_iter, (None, None))
            pending_edge_items = list(pending_edge_group) if pending_edge_group is not None else []

            for row_key, row_group in itertools.groupby(merged_rows, key=_pit_row_group_key):
                buffer = KeyHistoryBuffer(
                    storage=storage, buffer_limit=params.key_history_buffer, limits=limits
                )
                for row in row_group:
                    buffer.add(row)
                with buffer.rows() as key_row_iter:
                    key_rows = {row["revision_id"]: row for row in key_row_iter}
                records = tuple(
                    revision_record_from_row(key_rows[revision]) for revision in sorted(key_rows)
                )
                # The key's whole read closure (key_rows) — not just the window's own instants —
                # so this is exactly the ``earliest`` value _key_closure filtered ownership on.
                owner_at = min(row[column] for row in key_rows.values())

                if pending_edge_key is not None and pending_edge_key < row_key:
                    raise CatalogIntegrityError(
                        "a mapped edge references an observation_key with no corresponding "
                        "Canonical rows in this window"
                    )
                if pending_edge_key == row_key:
                    key_edges = tuple(
                        PrecedenceEvidence.model_validate(item["evidence"])
                        for item in pending_edge_items
                    )
                    pending_edge_key, next_group = next(edge_iter, (None, None))
                    pending_edge_items = list(next_group) if next_group is not None else []
                else:
                    key_edges = ()

                moved = effective_available_times(list(key_rows.values()), bound=bound_assumption)
                available = {
                    revision: moved[revision][1] if revision in moved else row["available_time"]
                    for revision, row in key_rows.items()
                }

                seen_revisions: set[str] = set()
                for selection in _evaluate(row_key, records, key_edges, spec, available):
                    lineage_out: SelectedRevisionLineage | None = None
                    gap_out: EvidenceGap | None = None
                    event_at: datetime | None = None
                    if selection.status is PointInTimeStatus.SELECTED:
                        revision = selection.selected_revision_id
                        if revision is None:  # pragma: no cover - the contract forbids it
                            raise CatalogIntegrityError("a selected result without a revision")
                        event_at = key_rows[revision][column]
                        if revision not in seen_revisions:
                            seen_revisions.add(revision)
                            source_row = key_rows[revision]
                            if revision in moved:
                                source_row = dict(source_row, available_time=moved[revision][1])
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
                # key_rows / records / key_edges / buffer go out of scope here, before the next
                # observation_key's group is even read off merged_rows.

            # Every mapped edge's observation_key must be one _mapped_edges found rows for (it
            # only ever adds an edge once both Raw endpoints resolved among that key's rows); an
            # edges group that never matched a row_key would mean the two runs disagree with
            # what by_key / edges actually held, which the spill/merge path must never do.
            if pending_edge_key is not None or next(edge_iter, None) is not None:
                raise CatalogIntegrityError(
                    "a mapped edge references an observation_key with no corresponding "
                    "Canonical rows in this window"
                )

    # ``_generate`` holds one root reader per run set and, mid-key, one
    # ``KeyHistoryBuffer.rows()`` context: closed explicitly here on every exit of the caller's
    # ``with`` block -- full iteration, an early ``break``, or an exception -- rather than left
    # to whenever the generator object is garbage collected.
    # ``.close()`` throws ``GeneratorExit`` in at the generator's current (or not yet started)
    # suspension point, which the ``with`` statements above unwind exactly as any other exit.
    generated = _generate()
    try:
        yield generated
    finally:
        generated.close()
