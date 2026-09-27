"""Multi-instrument in-sample validation: pooled G0 – G3 plus per-instrument G0 – G3, then G4.

Implementation note (Phase 8, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING). No core / contract /
Schema change: an ``OutcomeRequest`` stays single-instrument (its bars are one instrument's, see
``core.contracts.outcome``), so a backtest over several instruments is labelled with **one request
per instrument**, and the per-instrument ``OutcomeTable``\\ s are combined here. A label carries
its instrument in its ``event_key`` (``"<instrument>|<decision time>"``, the key
``research.strategies.validation`` gives every event), which is all pooling needs.

``pool_outcomes(tables)`` builds the pooled table and refuses (``ValueError``) anything that is
not correctly keyed: every label of ``tables[name]`` must name ``name`` in its key, every table
must be about the same outcome, label spec and provider. The pooled table is a hand-built
``OutcomeTable`` (``request_hash`` / ``provider_hash`` are ``None``, so it is never stored); its
``result_hash`` is the content hash of the per-instrument result hashes.

``run_multi_instrument_validation`` is the multi-instrument counterpart of
``g4.run_validation``:

1. **pooled** G0 → G3 (``run_in_sample`` on the pooled table, standard gate ids): the portfolio
   statistic. Pooling never loosens a rule: labels of different instruments that overlap in time
   count as dependent in ``effective_sample_size`` / ``overlap_lag`` (conservative), and the G3
   adjustment uses the unchanged ``family_trial_count`` (an instrument is not a trial);
2. **per instrument** (only when the pooled stages did not fail): ``run_in_sample`` on that
   instrument's own table and sides, each gate judged under its base id (so the Profile's
   ``inconclusive_bands`` apply unchanged) and recorded as ``<gate_id>.instrument.<name>``
   (``INSTRUMENT_INFIX``; ``reason_for_gate`` maps it by its base prefix). A validated instrument
   without labels gets ``G0.data_available.instrument.<name>`` = ``INCONCLUSIVE``
   (``instrument_labels``). Requiring every instrument's G3 as well as the pooled one is an
   intersection–union test: it can only remove PASSes, never add one;
3. G4 (``g4.robustness_stage``) only when nothing above failed.

ADR-0060 (2026-09-26): the C-T4 market benchmark and inverse control
(``research.validation.benchmark``) are computed **once, on the pooled input** — the
equal-weight buy-and-hold spans every validated instrument (``1 / N`` each) — and recorded under
the standard ids (``G2.market_benchmark.<rule>``, ``G2.inverse_control``). A per-instrument input
must not carry a ``benchmark`` source (refused, ``ValueError``): no per-instrument copies.

The report verdict stays ``derive_verdict`` of all gates, so the standard rule combines the
instruments: any ``FAIL`` fails, else any ``INCONCLUSIVE`` is ``INCONCLUSIVE``, and a PASS needs
the pooled evidence **and** every instrument's evidence to pass.
``MultiInstrumentRun.per_instrument`` records each instrument's own verdict and label count for
the report view.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from core.domain.base import content_hash
from core.domain.research import GateResult, Verdict, derive_verdict
from research.outcomes.table import OutcomeTable
from research.validation.g4 import RobustnessResult, RobustnessSource, robustness_stage
from research.validation.gates import inconclusive_gate
from research.validation.pipeline import InSampleInput, run_in_sample

__all__ = [
    "EVENT_KEY_SEPARATOR",
    "INSTRUMENT_INFIX",
    "InstrumentEvidence",
    "MultiInstrumentRun",
    "instrument_of",
    "pool_outcomes",
    "run_multi_instrument_validation",
]

#: Gate-id infix of a per-instrument gate: ``G2.effective_sample_size.instrument.<name>``.
INSTRUMENT_INFIX: Final = ".instrument."
#: Separator between the instrument and the decision time in an event key.
EVENT_KEY_SEPARATOR: Final = "|"


def instrument_of(event_key: str) -> str:
    """The instrument an event key names (the part before the first ``|``)."""
    instrument, separator, rest = event_key.partition(EVENT_KEY_SEPARATOR)
    if not separator or not instrument or not rest:
        raise ValueError(f"event key {event_key!r} does not name its instrument")
    return instrument


def pool_outcomes(tables: Mapping[str, OutcomeTable]) -> OutcomeTable:
    """One table holding every instrument's labels, refused unless correctly keyed."""
    if not tables:
        raise ValueError("nothing to pool: no instrument has an outcome table")
    first = next(iter(tables.values()))
    for name, table in tables.items():
        if (table.outcome, table.label_spec_hash, table.provider) != (
            first.outcome,
            first.label_spec_hash,
            first.provider,
        ):
            raise ValueError(f"{name}: outcome tables of different outcomes cannot be pooled")
        foreign = [label.event_key for label in table if instrument_of(label.event_key) != name]
        if foreign:
            raise ValueError(f"{name}: labels keyed to another instrument: {foreign[:3]}")
    names = sorted(tables)
    labels = sorted(
        (label for name in names for label in tables[name]),
        key=lambda label: (label.event_time, label.event_key),
    )
    return OutcomeTable(
        outcome=first.outcome,
        label_spec_hash=first.label_spec_hash,
        provider=first.provider,
        result_hash=content_hash(
            {"pooled_result_hashes": {n: tables[n].result_hash for n in names}}
        ),
        labels=tuple(labels),
    )


@dataclass(frozen=True, slots=True)
class InstrumentEvidence:
    """One instrument's own in-sample verdict (``None``: not evaluated, a pooled stage failed)."""

    instrument: str
    labels: int
    verdict: Verdict | None

    def to_dict(self) -> dict[str, object]:
        return {
            "labels": self.labels,
            "verdict": None if self.verdict is None else self.verdict.value,
        }


@dataclass(frozen=True)
class MultiInstrumentRun:
    """Pooled + per-instrument G0 – G3, then G4 (see module docs)."""

    gates: tuple[GateResult, ...]
    robustness: RobustnessResult | None
    per_instrument: tuple[InstrumentEvidence, ...]

    def view(self) -> dict[str, object]:
        return {item.instrument: item.to_dict() for item in self.per_instrument}


def _recorded(gate: GateResult, instrument: str) -> GateResult:
    return GateResult.model_validate(
        {**gate.model_dump(), "gate_id": f"{gate.gate_id}{INSTRUMENT_INFIX}{instrument}"}
    )


def _failed(gates: Sequence[GateResult]) -> bool:
    return any(gate.verdict is Verdict.FAIL for gate in gates)


def run_multi_instrument_validation(
    pooled: InSampleInput,
    per_instrument: Mapping[str, InSampleInput],
    instruments: Sequence[str],
    robustness: RobustnessSource,
) -> MultiInstrumentRun:
    """Pooled G0 → G3, per-instrument G0 → G3, then G4 unless a stage failed (module docs).

    ``instruments`` is the validated scope (at least two); ``per_instrument`` holds the input of
    each scoped instrument that has labels. Every per-instrument input must share the pooled
    context, seed and control seeds, and the per-instrument labels must be exactly the pooled
    labels; anything else is refused (``ValueError``).
    """
    scope = tuple(instruments)
    if len(scope) < 2 or len(set(scope)) != len(scope):
        raise ValueError("a multi-instrument validation needs at least two distinct instruments")
    if not set(per_instrument) <= set(scope):
        raise ValueError(
            f"instruments outside the scope: {sorted(set(per_instrument) - set(scope))}"
        )
    keys: list[str] = []
    for name, inp in per_instrument.items():
        if (inp.context, inp.seed, inp.control_seeds) != (
            pooled.context,
            pooled.seed,
            pooled.control_seeds,
        ):
            raise ValueError(f"{name}: a per-instrument input must share the pooled binding")
        if inp.benchmark is not None:
            raise ValueError(
                f"{name}: the market benchmark / inverse control are pooled only (ADR-0060); "
                "a per-instrument input takes no benchmark source"
            )
        mine = [label.event_key for label in inp.outcomes]
        if any(instrument_of(key) != name for key in mine):
            raise ValueError(f"{name}: labels keyed to another instrument")
        keys.extend(mine)
    if sorted(keys) != sorted(label.event_key for label in pooled.outcomes):
        raise ValueError("the per-instrument labels are not exactly the pooled labels")

    gates = run_in_sample(pooled)
    evidence: list[InstrumentEvidence] = []
    if _failed(gates):
        evidence = [
            InstrumentEvidence(name, _label_count(per_instrument, name), None) for name in scope
        ]
        return MultiInstrumentRun(gates=gates, robustness=None, per_instrument=tuple(evidence))
    recorded: list[GateResult] = []
    for name in scope:
        one = per_instrument.get(name)
        if one is None:
            gates_of: tuple[GateResult, ...] = (
                inconclusive_gate(
                    f"G0.data_available{INSTRUMENT_INFIX}{name}", "instrument_labels", 0.0
                ),
            )
        else:
            gates_of = tuple(_recorded(gate, name) for gate in run_in_sample(one))
        recorded.extend(gates_of)
        evidence.append(
            InstrumentEvidence(name, _label_count(per_instrument, name), derive_verdict(gates_of))
        )
    in_sample = (*gates, *recorded)
    if _failed(in_sample):
        return MultiInstrumentRun(gates=in_sample, robustness=None, per_instrument=tuple(evidence))
    run = robustness_stage(in_sample, pooled.context.profile, robustness)
    return MultiInstrumentRun(
        gates=run.gates, robustness=run.robustness, per_instrument=tuple(evidence)
    )


def _label_count(per_instrument: Mapping[str, InSampleInput], name: str) -> int:
    inp = per_instrument.get(name)
    return 0 if inp is None else len(inp.outcomes)
