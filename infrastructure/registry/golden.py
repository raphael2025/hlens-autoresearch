"""Golden input / output payloads of a Strategy Artifact (ADR-0005 §2 / §5; 2026-09-26).

``GoldenOutputs`` (``core.domain.artifact``) binds two blobs by URI + ``ContentHash``: the
reference **signal** sequence and the reference **position** sequence. The contract does not fix
their encoding; this module is the one encoding both the research-side packer
(``research.promotion``) and the production-side Equivalence Gate (``apps.promotion``) use, so it
lives in ``infrastructure/`` (both planes may import it; it imports only ``core``).

- **signals** (``hlens.registry.golden_signals@1.0.0``): the declared golden input set — the
  ``StrategyRequest`` s the strategy answers, each carrying its canonical ``SignalObservation``
  sequence. At the ``StrategyProvider`` level the "signals" of an artifact are exactly the signal
  observations the strategy consumes; storing the whole request (strategy ref, spec hash, fixed
  params, instruments, knowledge cutoff, decision times, signals) makes the set replayable by a
  production implementation without any research code.
- **positions** (``hlens.registry.golden_positions@1.0.0``): per golden request, in the same order,
  its ``request_hash`` and the answered ``TargetPosition`` s. The answering provider's identity is
  deliberately **not** part of the payload — a production implementation is a different provider
  and must reproduce the positions, not the research provider's name.

A payload's content hash is ``core.domain.base.content_hash`` (SHA-256 of canonical JSON); the
blob bytes are that canonical JSON. Decoding re-validates every item as its contract and requires
the payload to re-encode to itself byte for byte, so a hand-edited or non-canonical payload is
refused (``GoldenPayloadInvalid``), never normalized.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.strategy import StrategyRequest, StrategyResult, TargetPosition
from core.domain.base import canonical_json, content_hash

__all__ = [
    "GOLDEN_POSITIONS_SCHEMA",
    "GOLDEN_SCHEMA_VERSION",
    "GOLDEN_SIGNALS_SCHEMA",
    "GoldenAnswer",
    "GoldenPayloadInvalid",
    "decode_golden_positions",
    "decode_golden_signals",
    "golden_positions_payload",
    "golden_signals_payload",
    "payload_hash",
    "positions_json",
]

GOLDEN_SIGNALS_SCHEMA: Final = "hlens.registry.golden_signals"
GOLDEN_POSITIONS_SCHEMA: Final = "hlens.registry.golden_positions"
GOLDEN_SCHEMA_VERSION: Final = "1.0.0"


class GoldenPayloadInvalid(ValueError):
    """A golden payload is not exactly what this codec writes (shape, contract, canonical form)."""


@dataclass(frozen=True, slots=True)
class GoldenAnswer:
    """The golden answer to one golden request: its ``request_hash`` and positions."""

    request_hash: str
    positions: tuple[TargetPosition, ...]


def positions_json(positions: Sequence[TargetPosition]) -> list[Any]:
    """The JSON form of a position sequence (the unit the Equivalence Gate compares exactly)."""
    return [item.model_dump(mode="json") for item in positions]


def golden_signals_payload(requests: Sequence[StrategyRequest]) -> dict[str, Any]:
    """The signals payload of a golden input set (order preserved)."""
    if not requests:
        raise GoldenPayloadInvalid("a golden input set needs at least one request")
    return {
        "schema": GOLDEN_SIGNALS_SCHEMA,
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "requests": [request.model_dump(mode="json") for request in requests],
    }


def golden_positions_payload(results: Sequence[StrategyResult]) -> dict[str, Any]:
    """The positions payload of the answers to a golden input set (same order as the requests)."""
    if not results:
        raise GoldenPayloadInvalid("a golden answer set needs at least one result")
    return {
        "schema": GOLDEN_POSITIONS_SCHEMA,
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "answers": [
            {"request_hash": result.request_hash, "positions": positions_json(result.positions)}
            for result in results
        ],
    }


def _header(payload: Mapping[str, Any], schema: str, body: str) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise GoldenPayloadInvalid("a golden payload must be a JSON object")
    if set(payload) != {"schema", "schema_version", body}:
        raise GoldenPayloadInvalid(f"a {schema} payload has exactly schema, schema_version, {body}")
    if payload["schema"] != schema or payload["schema_version"] != GOLDEN_SCHEMA_VERSION:
        raise GoldenPayloadInvalid(
            f"expected {schema}@{GOLDEN_SCHEMA_VERSION}, "
            f"got {payload['schema']!r}@{payload['schema_version']!r}"
        )
    items = payload[body]
    if not isinstance(items, list) or not items:
        raise GoldenPayloadInvalid(f"{schema}.{body} must be a non-empty list")
    return items


def _same_bytes(payload: Mapping[str, Any], rebuilt: Mapping[str, Any], schema: str) -> None:
    if canonical_json(dict(payload)) != canonical_json(dict(rebuilt)):
        raise GoldenPayloadInvalid(f"the {schema} payload does not re-encode to itself")


def decode_golden_signals(payload: Mapping[str, Any]) -> tuple[StrategyRequest, ...]:
    """The golden requests of a signals payload; anything else is ``GoldenPayloadInvalid``."""
    items = _header(payload, GOLDEN_SIGNALS_SCHEMA, "requests")
    try:
        requests = tuple(
            StrategyRequest.model_validate_json(canonical_json(item)) for item in items
        )
    except ValidationError as exc:
        raise GoldenPayloadInvalid(f"a golden request is not a StrategyRequest: {exc}") from exc
    _same_bytes(payload, golden_signals_payload(requests), GOLDEN_SIGNALS_SCHEMA)
    hashes = [request.content_hash() for request in requests]
    if len(set(hashes)) != len(hashes):
        raise GoldenPayloadInvalid("the golden input set holds the same request twice")
    return requests


def decode_golden_positions(payload: Mapping[str, Any]) -> tuple[GoldenAnswer, ...]:
    """The golden answers of a positions payload; anything else is ``GoldenPayloadInvalid``."""
    items = _header(payload, GOLDEN_POSITIONS_SCHEMA, "answers")
    answers: list[GoldenAnswer] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"request_hash", "positions"}:
            raise GoldenPayloadInvalid("a golden answer has exactly request_hash and positions")
        request_hash, raw_positions = item["request_hash"], item["positions"]
        if not isinstance(request_hash, str) or not isinstance(raw_positions, list):
            raise GoldenPayloadInvalid("a golden answer has a bad request_hash or positions")
        try:
            positions = tuple(
                TargetPosition.model_validate_json(canonical_json(raw)) for raw in raw_positions
            )
        except ValidationError as exc:
            raise GoldenPayloadInvalid(f"a golden position is not a TargetPosition: {exc}") from exc
        answers.append(GoldenAnswer(request_hash, positions))
    rebuilt = {
        "schema": GOLDEN_POSITIONS_SCHEMA,
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "answers": [
            {"request_hash": a.request_hash, "positions": positions_json(a.positions)}
            for a in answers
        ],
    }
    _same_bytes(payload, rebuilt, GOLDEN_POSITIONS_SCHEMA)
    return tuple(answers)


def payload_hash(payload: Mapping[str, Any]) -> str:
    """The content hash of a golden payload (= SHA-256 of its blob bytes)."""
    return content_hash(dict(payload))
