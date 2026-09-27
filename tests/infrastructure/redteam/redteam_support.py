"""Shared attack helpers for the G2 cross-stage red-team suite.

Everything runs over the real stages joined on one catalog (``tests.infrastructure.dataset.
dataset_support.World``): D2 / D3E stores, the E1 normalizer, the D-33 reconciler, E2 listings, E3
reporters and the F3 builder. Only transports, clocks and the crash / tamper injection points are
test code. Fixtures stay tiny: two or three BTCUSDT trades, one or two exchangeInfo snapshots.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from core.contracts.universe import ResearchDatasetManifest
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS, DATASET_SELECTIONS
from infrastructure.dataset.builder import DatasetBuildError, DatasetBuilt
from infrastructure.dataset.manifests import manifest_row as _manifest_row
from infrastructure.pit.selector import PitConflictError, PitSpecError
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import QualityReporter, QualityReportError
from infrastructure.revision import RawRevisionStore
from infrastructure.revision.store import ArchiveIngested, IngestOutcome
from infrastructure.settings import local_file_uri_to_path
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseBuildError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, START, World
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.rest_store_support import SYMBOL, StepClock

#: After a first build the dataset's own table has a head; later specs must not bind it.
OWN: Final = (DATASET_SELECTIONS.table,)
OUTPUTS: Final = (DATASET_SELECTIONS.table, DATASET_MANIFESTS.table)


def build(
    w: World,
    spec: Any = None,
    *,
    data_type: str = "agg_trades",
    window: tuple[datetime, datetime] = (START, END),
    **spec_fields: Any,
) -> DatasetBuilt:
    """One F3 build; the spec defaults to every current head except the dataset's own table."""
    if spec is None:
        spec_fields.setdefault("skip", OWN)
        spec = w.spec(**spec_fields)
    return w.builder().build(FIRST_SLICE_UNIVERSE, spec, data_type, window[0], window[1])


def outputs(w: World) -> tuple[str | None, ...]:
    """Heads of the dataset's own tables: a refused attack must leave them exactly as they were."""
    return tuple(w.h.head(table) for table in OUTPUTS)


def manifests(w: World) -> list[dict[str, Any]]:
    return w.h.rows(DATASET_MANIFESTS)


def shape(manifest: ResearchDatasetManifest) -> dict[str, Any]:
    """A manifest without the catalog-local parts (snapshot ids and the report ids they derive).

    Two catalogs never share snapshot ids, so across worlds only this shape can be compared.
    """
    document: dict[str, Any] = json.loads(manifest.model_dump_json())
    document["dataset"].pop("snapshot_id")
    document["point_in_time"].pop("snapshot_bindings")
    document.pop("quality_report_ids")
    for gap in document["evidence_gaps"]:
        gap.pop("quality_report_id")
    return document


def dataset_rows(built: DatasetBuilt) -> list[dict[str, Any]]:
    """The materialized rows without the (spec-hash derived) selection id."""
    return [
        {name: value for name, value in row.items() if name != "selection_id"}
        for row in built.selection.rows
    ]


# =========================================================================================
# market data
# =========================================================================================


def archive_trades(
    w: World,
    lines: list[str],
    *,
    knowledge: datetime,
    day: date = ss.DAY,
    retrieved_at: datetime = c.ARCHIVE_RETRIEVED,
    request_id: str = "archive-1",
    adapter: Any = None,
    microbatch_rows: int | None = None,
) -> IngestOutcome:
    """Publish one agg_trades archive and ingest it through the real D2 store."""
    arc = rs.archive(
        w.h.storage,
        data_type="agg_trades",
        symbol=SYMBOL,
        day=day,
        rows=lines,
        retrieved_at=retrieved_at,
        request_id=request_id,
    )
    kwargs: dict[str, Any] = {}
    if microbatch_rows is not None:
        kwargs["microbatch_rows"] = microbatch_rows
    store = RawRevisionStore(
        w.h.adapter if adapter is None else adapter,
        w.h.storage,
        clock=StepClock(start=knowledge),
        **kwargs,
    )
    return store.ingest(arc.collected, arc.context)


def archived(outcome: IngestOutcome) -> str:
    assert isinstance(outcome, ArchiveIngested), outcome
    return outcome.archive_revision_id


def rest_trades(
    w: World,
    items: list[dict[str, Any]],
    *,
    knowledge: datetime,
    t0: int = ss.T0,
    retrieved_ms: int = cs.RETRIEVED_AT_MS,
    request_id: str = "req-rest",
    adapter: Any = None,
    element_microbatch_rows: int | None = None,
) -> list[str]:
    """Collect one REST aggTrades chain (D3D mock venue) and store it through D3E."""
    cs.queue_agg_chain(w.h.venue, SYMBOL, t0, [items])
    request = ss.agg_request(request_id, start_ms=t0)
    collected = w.h.collect(request, start_ms=retrieved_ms)
    assert not isinstance(collected, Exception), collected
    stored = w.h.store(
        clock=StepClock(start=knowledge),
        adapter=adapter,
        element_microbatch_rows=element_microbatch_rows,
    ).ingest_collection(request)
    return [page.response_revision_id for page in stored.pages]


def normalize(
    w: World,
    raw_table: str,
    unit: str,
    *,
    at: datetime,
    adapter: Any = None,
    microbatch_rows: int | None = None,
) -> Any:
    return c.normalizer(
        w.h, clock=StepClock(start=at), adapter=adapter, microbatch_rows=microbatch_rows
    ).normalize_unit(raw_table, unit)


def reconcile(w: World, *, at: datetime, day: date = ss.DAY, adapter: Any = None) -> Any:
    return w.h.reconciler(clock=StepClock(start=at), adapter=adapter).reconcile(
        "agg_trades", SYMBOL, day
    )


def replacement_lines(items: list[dict[str, Any]]) -> list[str]:
    """The same trades as a *different* archive object: one price differs (a new checksum)."""
    lines = ss.archive_agg_lines(items)
    lines[0] = lines[0].replace(f",{items[0]['p']},", ",99999.00000000,")
    assert lines != ss.archive_agg_lines(items)
    return lines


# =========================================================================================
# tampering below the stores
# =========================================================================================


def data_files(w: World, table: str, snapshot_id: str) -> list[str]:
    """Local paths of the Parquet data files a snapshot of ``table`` reads."""
    namespace, name = table.split(".")
    iceberg = w.h.catalog.sql_catalog().load_table((namespace, name))
    return [
        str(local_file_uri_to_path(task.file.file_path, field_name="data_file"))
        for task in iceberg.scan(snapshot_id=int(snapshot_id)).plan_files()
    ]


def rewrite_in_place(
    w: World,
    definition: RegisteredTableDefinition,
    snapshot_id: str,
    column: str,
    mutate: Mapping[Any, Any],
) -> int:
    """Rewrite the snapshot's data files in place (same path, same schema, field ids kept).

    Values of ``column`` found in ``mutate`` are replaced; returns how many cells changed. This
    is storage corruption under an immutable snapshot, not a catalog commit.
    """
    changed = 0
    for path in data_files(w, definition.table, snapshot_id):
        table = pq.read_table(path)
        if column not in table.column_names:
            continue
        values = table.column(column).to_pylist()
        new = [mutate.get(value, value) for value in values]
        hits = sum(1 for old, value in zip(values, new, strict=True) if old != value)
        if not hits:
            continue
        index = table.column_names.index(column)
        field = table.schema.field(index)
        table = table.set_column(index, field, pa.array(new, type=field.type))
        pq.write_table(table, path)
        changed += hits
    return changed


#: What a refusal anywhere between Raw and the manifest looks like (fail closed, typed).
REFUSALS: Final = (
    CatalogIntegrityError,
    DatasetBuildError,
    PitConflictError,
    PitSpecError,
    QualityReportError,
    UniverseBuildError,
)


def report(
    w: World,
    *,
    at: datetime,
    data_type: str = "agg_trades",
    days: tuple[date, ...] = (ss.DAY,),
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    listing: bool = True,
) -> list[str]:
    """``World.report`` with an explicit report clock (for data known after the default one)."""
    reporter = QualityReporter(w.h.adapter, w.h.storage, clock=StepClock(start=at))
    ids = [reporter.report(data_type, symbol, day).report_id for symbol in symbols for day in days]
    if listing:
        listing_reporter = ListingQualityReporter(
            w.h.adapter, w.h.storage, market_data_base_url=ds.ORIGIN, clock=StepClock(start=at)
        )
        ids.append(listing_reporter.report().report_id)
    return ids


def manifest_row(manifest: ResearchDatasetManifest) -> dict[str, Any]:
    return _manifest_row(manifest)
