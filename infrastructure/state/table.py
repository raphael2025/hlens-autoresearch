"""State table: an in-memory Arrow materialization of one ``StateResult`` (Phase 2; ADR-0035 §4).

``state_table(spec, request, result)`` returns one row per evaluation time with the state label
(``null`` = not computable), the inputs used and the latest input time, plus the identity columns
that make the table reproducible: ``state_ref``, ``spec_hash``, ``provider``, ``request_hash`` and
``result_hash``. The result is checked against the request and spec first
(``StateResult.check_answers`` needs the descriptor, so the caller passes it).

Writing the table into the catalog (a ``state.*`` Iceberg table) is not part of this framework
batch.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]

from core.contracts.state import StateProviderDescriptor, StateRequest, StateResult
from core.domain.specs import StateSpec

__all__ = ["STATE_TABLE_SCHEMA", "state_table"]

STATE_TABLE_SCHEMA: Any = pa.schema(
    [
        pa.field("state_ref", pa.string(), nullable=False),
        pa.field("spec_hash", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("request_hash", pa.string(), nullable=False),
        pa.field("result_hash", pa.string(), nullable=False),
        pa.field("evaluation_time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("state", pa.string(), nullable=True),
        pa.field("inputs_used", pa.int64(), nullable=False),
        pa.field("latest_input_time", pa.timestamp("us", tz="UTC"), nullable=True),
    ]
)


def state_table(
    spec: StateSpec,
    request: StateRequest,
    result: StateResult,
    descriptor: StateProviderDescriptor,
) -> Any:
    """The ``pyarrow.Table`` of ``result`` (checked against request, spec and descriptor)."""
    result.check_answers(request, descriptor, spec)
    rows = len(result.values)
    return pa.table(
        {
            "state_ref": [str(spec.ref)] * rows,
            "spec_hash": [request.spec_hash] * rows,
            "provider": [result.provider] * rows,
            "request_hash": [result.request_hash] * rows,
            "result_hash": [result.result_hash] * rows,
            "evaluation_time": [item.evaluation_time for item in result.values],
            "state": [item.state for item in result.values],
            "inputs_used": [item.inputs_used for item in result.values],
            "latest_input_time": [item.latest_input_time for item in result.values],
        },
        schema=STATE_TABLE_SCHEMA,
    )
