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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, time, timedelta
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
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.channel_reconcile import ChannelReconciler, revision_record_from_row
from infrastructure.revision.precedence import maximal_heads
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "PIT_BINDING",
    "PIT_SPEC",
    "REQUIRED_BINDINGS",
    "EvidenceGap",
    "PitConflictError",
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
        self._edges: dict[tuple[str, str, date], tuple[Any, ...]] = {}

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
        days: Sequence[date],
        by_key: Mapping[str, Sequence[Mapping[str, Any]]],
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
        mapped: dict[str, list[PrecedenceEvidence]] = {}
        for day in days:
            verified = self._edges.get((data_type, symbol, day))
            if verified is None:
                verified = tuple(reconciler.verified_edges(data_type, symbol, day))
                self._edges[(data_type, symbol, day)] = verified
            for edge in verified:
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
    cutoff = spec.knowledge_cutoff
    if spec.simulation_time is not None:
        instants = [spec.simulation_time]
    else:
        start, end = spec.simulation_start, spec.simulation_end
        if start is None or end is None:  # pragma: no cover - the contract forbids it
            raise PitSpecError("the spec has neither a simulation time nor an interval")
        changes = {
            available[item.revision_id]
            for item in records
            if item.availability.times.knowledge_time <= cutoff
            and start < available[item.revision_id] < end
        }
        instants = [start, *sorted(changes)]
    results: list[PointInTimeSelection] = []
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
        results.append(
            PointInTimeSelection(
                observation_key=key,
                simulation_time=at,
                knowledge_cutoff=cutoff,
                status=status,
                selected_revision_id=heads[0] if status is PointInTimeStatus.SELECTED else None,
                maximal_heads=heads,
            )
        )
    return results
