"""FeatureObservations from PIT-selected Canonical bars and derived bars (Phase 1 F4; ADR-0030).

Two builders turn what the F1 selector proved into ``FeatureObservation``s with lineage, and
``pit_feature_request`` binds them into a ``FeatureRequest`` whose evaluation times the PIT view
can answer:

- ``bar_observations(selection, spec)``: one observation per revision that is **selected** at some
  evaluation of a conflict-free ``canonical.bars_1m`` selection (``PitSelector.select`` under
  ``spec``). Key, interval, ``available_time`` and ``knowledge_time`` are the Canonical row's;
  values are the canonical symbol and the bar's market content; lineage is the selector's
  ``SelectedRevisionLineage``;
- ``derived_bar_observations(bars, selection)``: one observation per **complete** E4 derived bar
  (an incomplete bar is left out — a gap for the features, never a filled bar). Its key names the
  resample rule, symbol, period and start; ``available_time`` / ``knowledge_time`` are the derived
  bar's; values add ``minutes`` and ``resample_content`` (``sha256:`` + the derived bar's content
  hash, which binds every constituent revision id). ``FeatureObservation`` carries one lineage, so a
  derived bar carries its **closing** constituent's lineage; the full constituent list is bound by
  ``resample_content`` and every constituent's lineage stays in ``selection.lineage``.

Why every selected revision, and when a request may be evaluated: ``FeatureRequest.visible_at``
keeps, per key, the latest available observation. For a conflict-free selection each change of a
key's selection inside the simulation interval happens at the ``available_time`` of the newly
selected revision (a new head can only be the revision that just became available), so that rule
reproduces the PIT selection at ``t - available_lag`` exactly — for ``t - available_lag`` inside
the simulation interval of the spec (``[simulation_start, simulation_end)``, or
``>= simulation_time`` for a point spec, where every selected revision is already available).
Outside it the selection is not known (a revision superseded before the interval start is not in
the selection), so ``pit_feature_request`` refuses such evaluation times instead of answering from
a view that would use later knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping
from core.domain.specs import FeatureSpec
from infrastructure.canonical import rules
from infrastructure.canonical.resample import RESAMPLE_ID, RESAMPLE_VERSION, DerivedBar
from infrastructure.pit.selector import PitSelection

__all__ = [
    "BAR_VALUE_COLUMNS",
    "FeatureInputBuildError",
    "bar_observations",
    "derived_bar_observations",
    "pit_feature_request",
]

#: Canonical bar columns carried as observation values (next to ``symbol``).
BAR_VALUE_COLUMNS: Final = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)
_BARS_TABLE: Final = rules.CANONICAL_TABLES["klines_1m"].table


class FeatureInputBuildError(ValueError):
    """The PIT selection cannot honestly feed a feature request (fail closed)."""


def _check_selection(selection: PitSelection, spec: PointInTimeSpec) -> None:
    if not isinstance(selection, PitSelection) or not isinstance(spec, PointInTimeSpec):
        raise FeatureInputBuildError("expected a PitSelection and its PointInTimeSpec")
    if selection.canonical_table != _BARS_TABLE:
        raise FeatureInputBuildError(f"only {_BARS_TABLE} selections feed bar observations")
    if selection.conflicts:
        raise FeatureInputBuildError(
            f"{len(selection.conflicts)} key(s) have competing heads: nothing may be computed"
        )
    for item in selection.selections:
        if item.knowledge_cutoff != spec.knowledge_cutoff:
            raise FeatureInputBuildError("the selection was made under another knowledge_cutoff")
        if spec.simulation_time is not None:
            inside = item.simulation_time == spec.simulation_time
        else:
            start, end = spec.simulation_start, spec.simulation_end
            inside = start is not None and end is not None and start <= item.simulation_time < end
        if not inside:
            raise FeatureInputBuildError(
                "the selection was evaluated outside the spec's simulation"
            )


def _lineage(selection: PitSelection) -> Mapping[str, SelectedRevisionLineage]:
    return {item.canonical_revision_id: item for item in selection.lineage}


def bar_observations(
    selection: PitSelection, spec: PointInTimeSpec
) -> tuple[FeatureObservation, ...]:
    """Every revision selected at some evaluation of ``selection``, as a bar observation."""
    _check_selection(selection, spec)
    lineage = _lineage(selection)
    selected = sorted(
        {
            item.selected_revision_id
            for item in selection.selections
            if item.status is PointInTimeStatus.SELECTED and item.selected_revision_id is not None
        }
    )
    out = []
    for revision in selected:
        row = selection.selected_rows[revision]
        out.append(
            FeatureObservation(
                observation_key=row["observation_key"],
                event_time=row["interval_start"],
                event_end_time=row["interval_end"],
                available_time=row["available_time"],
                knowledge_time=row["knowledge_time"],
                values=FrozenMapping(_bar_values(row)),
                lineage=lineage[revision],
            )
        )
    return tuple(out)


def _bar_values(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"symbol": row["symbol"], **{name: row[name] for name in BAR_VALUE_COLUMNS}}


def derived_bar_observations(
    bars: Iterable[DerivedBar], selection: PitSelection, spec: PointInTimeSpec
) -> tuple[FeatureObservation, ...]:
    """Every complete derived bar (from ``resample_bars(selection, ...)``) as an observation."""
    _check_selection(selection, spec)
    if spec.simulation_time is None:
        raise FeatureInputBuildError("derived bars come from a point selection (E4)")
    lineage = _lineage(selection)
    out = []
    for bar in bars:
        if not isinstance(bar, DerivedBar):
            raise FeatureInputBuildError("expected DerivedBar instances")
        if not bar.complete:
            continue
        missing = [revision for revision in bar.constituents if revision not in lineage]
        if missing:
            raise FeatureInputBuildError(
                f"derived bar {bar.interval_start.isoformat()} has constituents outside the "
                "selection"
            )
        out.append(
            FeatureObservation(
                observation_key=(
                    f"{RESAMPLE_ID}@{RESAMPLE_VERSION}:{bar.symbol}:{bar.minutes}m:"
                    f"{bar.interval_start.isoformat()}"
                ),
                event_time=bar.interval_start,
                event_end_time=bar.interval_end,
                available_time=bar.available_time,
                knowledge_time=bar.knowledge_time,
                values=FrozenMapping(
                    {
                        "symbol": bar.symbol,
                        **{name: getattr(bar, name) for name in BAR_VALUE_COLUMNS},
                        "minutes": bar.minutes,
                        "resample_content": f"sha256:{bar.content_sha256}",
                    }
                ),
                lineage=lineage[bar.constituents[-1]],
            )
        )
    return tuple(out)


def _check_evaluation_times(
    spec: PointInTimeSpec, available_lag: timedelta, evaluation_times: Sequence[datetime]
) -> None:
    for at in evaluation_times:
        view = at - available_lag
        if spec.simulation_time is not None:
            ok = view >= spec.simulation_time
        else:
            start, end = spec.simulation_start, spec.simulation_end
            ok = start is not None and end is not None and start <= view < end
        if not ok:
            raise FeatureInputBuildError(
                f"evaluation time {at.isoformat()} needs the PIT view at {view.isoformat()}, "
                "which the spec's simulation does not cover"
            )


def pit_feature_request(
    *,
    pit_spec: PointInTimeSpec,
    observations: Sequence[FeatureObservation],
    feature: FeatureSpec,
    evaluation_times: Sequence[datetime],
    manifest_content_hash: str,
) -> FeatureRequest:
    """A request for ``feature`` over observations built under ``pit_spec``.

    ``knowledge_cutoff`` is the spec's; every evaluation time must be answerable by its PIT view.
    ``manifest_content_hash`` is taken as given (ad-hoc / test runs): a request over a Research
    Dataset is built by ``infrastructure.feature.dataset.feature_request_from_dataset``, which
    loads and verifies the manifest and proves the observations against it (G2 RT-6).
    """
    if not isinstance(feature, FeatureSpec):
        raise FeatureInputBuildError("feature must be a FeatureSpec")
    _check_evaluation_times(pit_spec, feature.available_lag, evaluation_times)
    return FeatureRequest(
        feature=feature.ref,
        spec_hash=feature.content_hash(),
        manifest_content_hash=manifest_content_hash,
        knowledge_cutoff=pit_spec.knowledge_cutoff,
        evaluation_times=tuple(evaluation_times),
        observations=tuple(observations),
    )
