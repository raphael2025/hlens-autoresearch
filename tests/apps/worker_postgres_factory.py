"""PostgreSQL + Iceberg runtime factory for the W2 host integration test only."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from apps.worker.jobs import JobRunner
from infrastructure.catalog import connect_postgres_catalog
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter
from infrastructure.event.iceberg import EventTable
from infrastructure.event.table_definition import PHASE3_REGISTRY, ensure_event_tables
from infrastructure.event_bus.file import FileEventBus
from infrastructure.settings import Settings
from tests.infrastructure.event.test_event_iceberg import _cross


@contextmanager
def build_runtime() -> Iterator[JobRunner]:
    """Open the dedicated catalog, the test warehouse, durable bus, and durable job results."""
    warehouse = Path(os.environ["HLENS_TEST_WORKER_WAREHOUSE"])
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        catalog_uri=SecretStr(os.environ["HLENS_TEST_CATALOG_URI"]),
        catalog_name=os.environ["HLENS_TEST_WORKER_CATALOG_NAME"],
        warehouse_uri=warehouse.as_uri(),
        staging_uri=(warehouse.parent / "staging").as_uri(),
    )
    adapter = PyIcebergCatalogAdapter(connect_postgres_catalog(settings), PHASE3_REGISTRY)
    try:
        ensure_event_tables(adapter)
        table = EventTable(adapter)
        bus_root = Path(os.environ["HLENS_TEST_WORKER_BUS"])
        results = Path(os.environ["HLENS_TEST_WORKER_RESULTS"])
        invocations = Path(os.environ["HLENS_TEST_WORKER_INVOCATIONS"])
        ack_entered = Path(os.environ["HLENS_TEST_WORKER_ACK_ENTERED"])
        ack_completed = Path(os.environ["HLENS_TEST_WORKER_ACK_COMPLETED"])
        ready = Path(os.environ["HLENS_TEST_WORKER_READY"])

        def persist_event(_params: Mapping[str, Any]) -> dict[str, Any]:
            event_result = _cross()
            written = table.write(event_result)
            with invocations.open("a", encoding="utf-8") as stream:
                stream.write(event_result.result_hash + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return {
                "result_hash": event_result.result_hash,
                "snapshot_id": written.snapshot_id,
                "row_count": written.row_count,
            }

        class AckProbe:
            """Pause at JobRunner's result-before-ack boundary in the first child process."""

            def __init__(self, bus: FileEventBus) -> None:
                self.bus = bus

            def publish(self, message: Any) -> None:
                self.bus.publish(message)

            def poll(self, consumer: str, topic: str, limit: int) -> Any:
                return self.bus.poll(consumer, topic, limit)

            def ack(self, consumer: str, topic: str, message_id: str) -> None:
                ack_entered.write_text("entered\n", encoding="utf-8")
                if os.environ.get("HLENS_TEST_WORKER_BLOCK_BEFORE_ACK") == "1":
                    threading.Event().wait()
                self.bus.ack(consumer, topic, message_id)
                ack_completed.write_text("acked\n", encoding="utf-8")

        with FileEventBus(bus_root) as bus:
            ready.write_text("ready\n", encoding="utf-8")
            yield JobRunner(
                AckProbe(bus),
                consumer="w2-postgres-worker-test",
                topic="worker.w2.integration",
                handlers={"test.persist_event": persist_event},
                results=results,
            )
    finally:
        adapter.close()
