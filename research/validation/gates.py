"""Gate building blocks: every threshold is read from the bound ValidationProfile (ADR-0037 §3).

``threshold(profile, path)`` resolves a Profile field path such as
``cost_stress.stress_multipliers[0]`` and returns its value **together with the path**, so a
``GateResult`` can never carry a threshold without the Profile field it came from
(07-validation.md §2). Nothing in this package has a default threshold: a missing field is an
error, not a fallback.

Comparators are part of the metric name (``"...[>=]"`` / ``"...[<=]"``) so a verifier holding the
Profile can recompute the verdict. ``ValidationProfile.inconclusive_bands`` is keyed by
``gate_id``: when ``|value - threshold| <= band`` the gate is ``INCONCLUSIVE`` instead of a
knife-edge PASS / FAIL (ADR-0037 §3; the band values are Profile numbers, TBD).

Phase 8 (ADR-0041): a path the Profile contract does not have raises ``ProfileFieldMissing``. Some
robustness rules have **no** Profile field yet (capacity, cross-asset consistency, the sealed OOS
budget); for those a caller may pass an ``explicit_threshold`` whose source is ``param:<name>`` (a
verifier sees it did not come from the Profile), or nothing — then the gate is
``missing_field_gate``: ``INCONCLUSIVE`` with metric ``profile_field_missing:<name>``, never a
default and never a PASS.

Review fixes (ADR-0041 implementation note, 2026-09-25): a Constitution-required check whose
configuration is empty or disabled (no parameter neighbours, no time-alignment offsets, a zero
delay stress, no declared instrument scope) is ``configuration_missing_gate``: ``INCONCLUSIVE``
with metric ``configuration_missing:<what>`` — the check is never silently skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict

__all__ = [
    "CONFIGURATION_MISSING",
    "PARAM_SOURCE_PREFIX",
    "PROFILE_FIELD_MISSING",
    "Direction",
    "ProfileFieldMissing",
    "Threshold",
    "compare_gate",
    "configuration_missing_gate",
    "explicit_threshold",
    "flag_gate",
    "inconclusive_gate",
    "missing_field_gate",
    "profile_value",
    "threshold",
]

#: Metric prefix of a gate whose rule has no Profile field and no explicit parameter.
PROFILE_FIELD_MISSING: Final = "profile_field_missing"
#: Metric prefix of a Constitution-required check whose configuration is empty or disabled.
CONFIGURATION_MISSING: Final = "configuration_missing"
#: ``threshold_source`` prefix of a threshold passed as an explicit parameter (not the Profile).
PARAM_SOURCE_PREFIX: Final = "param:"


class ProfileFieldMissing(ValueError):
    """The Profile contract has no field at the requested path (no default is ever invented)."""


_PATH: Final = re.compile(r"^(?P<attrs>[a-z_]+(?:\.[a-z_]+)*)(?:\[(?P<index>\d+)\])?$")


def profile_value(profile: ValidationProfile, path: str) -> object:
    """The value at ``path`` (dotted attributes, optional trailing ``[index]``)."""
    match = _PATH.fullmatch(path)
    if match is None:
        raise ValueError(f"not a Profile field path: {path!r}")
    value: object = profile
    for name in match.group("attrs").split("."):
        model = type(value)
        if name not in model.model_fields:  # type: ignore[attr-defined]
            raise ProfileFieldMissing(f"the Profile has no field {path!r}")
        value = getattr(value, name)
        if value is None and name in getattr(model, "_FIELDS_SINCE", {}):
            # An optional ADR-0052 field this Profile does not carry is a missing field, exactly
            # as before the field existed (ADR-0041 §1 behaviour of an old Profile unchanged).
            raise ProfileFieldMissing(f"the Profile has no field {path!r}")
    index = match.group("index")
    if index is not None:
        if not isinstance(value, tuple):
            raise ValueError(f"{path!r} indexes a field that is not a sequence")
        if int(index) >= len(value):
            raise ValueError(f"{path!r} is out of range")
        value = value[int(index)]
    return value


@dataclass(frozen=True)
class Threshold:
    """A Profile threshold and the field it was read from."""

    value: float
    source: str


def threshold(profile: ValidationProfile, path: str) -> Threshold:
    value = profile_value(profile, path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{path!r} is not a numeric Profile field")
    return Threshold(value=float(value), source=path)


def explicit_threshold(name: str, value: float) -> Threshold:
    """A threshold for a rule without a Profile field, recorded as ``param:<name>``."""
    if not name.strip():
        raise ValueError("an explicit threshold needs a name")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"explicit threshold {name!r} must be numeric")
    return Threshold(value=float(value), source=f"{PARAM_SOURCE_PREFIX}{name}")


class Direction(StrEnum):
    AT_LEAST = ">="
    AT_MOST = "<="


def compare_gate(
    profile: ValidationProfile,
    gate_id: str,
    metric: str,
    value: float,
    limit: Threshold,
    direction: Direction,
) -> GateResult:
    """PASS / FAIL against a Profile threshold; the Profile band may make it INCONCLUSIVE."""
    if direction is Direction.AT_LEAST:
        passed = value >= limit.value
    else:
        passed = value <= limit.value
    verdict = Verdict.PASS if passed else Verdict.FAIL
    band = profile.inconclusive_bands.get(gate_id)
    if band is not None:
        if band < 0:
            raise ValueError(f"inconclusive band of {gate_id} must be >= 0")
        if abs(value - limit.value) <= band:
            verdict = Verdict.INCONCLUSIVE
    return GateResult(
        gate_id=gate_id,
        metric=f"{metric}[{direction.value}]",
        value=value,
        threshold=limit.value,
        threshold_source=limit.source,
        verdict=verdict,
    )


def flag_gate(gate_id: str, metric: str, ok: bool, value: float) -> GateResult:
    """A structural check without a numeric threshold (a rule of the Constitution / a binding)."""
    return GateResult(
        gate_id=gate_id,
        metric=metric,
        value=value,
        verdict=Verdict.PASS if ok else Verdict.FAIL,
    )


def inconclusive_gate(gate_id: str, metric: str, value: float) -> GateResult:
    """Insufficient evidence materialized as a gate (ADR-0013: never silently a PASS)."""
    return GateResult(gate_id=gate_id, metric=metric, value=value, verdict=Verdict.INCONCLUSIVE)


def missing_field_gate(gate_id: str, field: str, value: float = 0.0) -> GateResult:
    """A rule whose threshold has neither a Profile field nor an explicit parameter."""
    return inconclusive_gate(gate_id, f"{PROFILE_FIELD_MISSING}:{field}", value)


def configuration_missing_gate(gate_id: str, configuration: str, value: float = 0.0) -> GateResult:
    """A required check that cannot run because its configuration is empty or disabled."""
    return inconclusive_gate(gate_id, f"{CONFIGURATION_MISSING}:{configuration}", value)
