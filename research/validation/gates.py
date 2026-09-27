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

Exact thresholds (ADR-0052 §1, D-FLOAT; implementation note 2026-09-26): ``threshold`` returns an
exact ``Threshold`` (``exact`` set, ``source`` = the exact field's path) when the Profile carries
the field's ``*_exact`` sibling, or when the field itself is an ``ExactDecimal`` (the ADR-0052 §2 /
§3 fields). ``compare_gate`` then compares **exactly**: the computed value is quantized by the
versioned rule ``GATE_VALUE_QUANTIZATION`` (``quantize_gate_value``: the float's exact binary
value rounded half-even to ``GATE_VALUE_FRACTION_DIGITS`` fractional digits — a representation
rule, not a threshold), compared as ``Decimal`` with the exact threshold and the exact
inconclusive band (``inconclusive_bands_exact``; a float-only band is read as its shortest
round-trip text), and recorded as ``value_exact`` / ``threshold_exact`` (the floats derived from
them). A Profile without exact fields takes the float path, bit-identical to before.

Profile fields and explicit parameters (ADR-0052 §2, C-A4): ``sourced_threshold`` /
``sourced_parameter`` take a rule's value from the Profile when the Profile carries the field, and
then refuse an explicit ``param:`` value given at the same time (``ExplicitParamRefused``: a
researcher cannot override a Profile threshold). Without the field (every Profile written before
ADR-0052) the ADR-0041 §1 behaviour is unchanged: the explicit ``param:`` value, or nothing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.base import exact_decimal
from core.domain.research import GateResult, Verdict

__all__ = [
    "CONFIGURATION_MISSING",
    "GATE_VALUE_FRACTION_DIGITS",
    "GATE_VALUE_QUANTIZATION",
    "PARAM_SOURCE_PREFIX",
    "PROFILE_FIELD_MISSING",
    "Direction",
    "ExplicitParamRefused",
    "ProfileFieldMissing",
    "Threshold",
    "compare_gate",
    "configuration_missing_gate",
    "explicit_threshold",
    "flag_gate",
    "inconclusive_gate",
    "missing_field_gate",
    "profile_has",
    "profile_value",
    "quantize_gate_value",
    "sourced_parameter",
    "sourced_threshold",
    "threshold",
]

#: Metric prefix of a gate whose rule has no Profile field and no explicit parameter.
PROFILE_FIELD_MISSING: Final = "profile_field_missing"
#: Metric prefix of a Constitution-required check whose configuration is empty or disabled.
CONFIGURATION_MISSING: Final = "configuration_missing"
#: ``threshold_source`` prefix of a threshold passed as an explicit parameter (not the Profile).
PARAM_SOURCE_PREFIX: Final = "param:"


#: The versioned quantization rule of an exact gate value (ADR-0052 §1). A representation rule,
#: frozen with its version: changing the digits or the rounding is a new rule version.
GATE_VALUE_QUANTIZATION: Final = "hlens.validation.gate-value-quantization@1.0.0"
#: Fractional digits kept by ``GATE_VALUE_QUANTIZATION`` (representation, not a threshold).
GATE_VALUE_FRACTION_DIGITS: Final = 12
_QUANTUM: Final = Decimal(1).scaleb(-GATE_VALUE_FRACTION_DIGITS)
#: Enough digits for any finite double (<= 309 integer digits) plus the fractional digits.
_QUANTIZE_PRECISION: Final = 400


class ProfileFieldMissing(ValueError):
    """The Profile contract has no field at the requested path (no default is ever invented)."""


class ExplicitParamRefused(ValueError):
    """An explicit ``param:`` value for a rule whose value the bound Profile supplies (C-A4)."""


def quantize_gate_value(value: float | Decimal | int) -> Decimal:
    """``GATE_VALUE_QUANTIZATION``: the exact value of ``value`` (a float's exact binary value,
    never its decimal repr) rounded half-even to ``GATE_VALUE_FRACTION_DIGITS`` fractional digits,
    in canonical ``ExactDecimal`` form. NaN / ±Infinity and booleans are refused."""
    if isinstance(value, bool):
        raise ValueError("a gate value is a number, not a bool")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("a gate value must be finite (ADR-0013)")
        exact = Decimal(value)
    elif isinstance(value, Decimal | int):
        exact = Decimal(value)
        if not exact.is_finite():
            raise ValueError("a gate value must be finite (ADR-0013)")
    else:
        raise ValueError(f"a gate value must be numeric, not {type(value).__name__}")
    with localcontext() as context:
        context.prec = _QUANTIZE_PRECISION
        try:
            quantized = exact.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
        except InvalidOperation as exc:  # pragma: no cover - beyond any finite double
            raise ValueError(f"cannot quantize {value!r}") from exc
    return exact_decimal(quantized)


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


def profile_has(profile: ValidationProfile, path: str) -> bool:
    """Whether the Profile **carries** a value at ``path`` (an optional ADR-0052 field that is
    absent, or whose parent block is absent, does not count). Unknown paths raise as in
    ``profile_value``."""
    match = _PATH.fullmatch(path)
    if match is None:
        raise ValueError(f"not a Profile field path: {path!r}")
    value: object = profile
    for name in match.group("attrs").split("."):
        if value is None:
            return False
        if name not in type(value).model_fields:  # type: ignore[attr-defined]
            raise ProfileFieldMissing(f"the Profile has no field {path!r}")
        value = getattr(value, name)
    return value is not None


@dataclass(frozen=True)
class Threshold:
    """A Profile threshold and the field it was read from.

    ``exact`` (ADR-0052 §1): the exact threshold when the Profile field is exact; ``value`` is then
    ``float(exact)`` and ``source`` the exact field's path. ``None``: a float threshold (unchanged).
    """

    value: float
    source: str
    exact: Decimal | None = None


def _exact_sibling_path(path: str) -> str:
    match = _PATH.fullmatch(path)
    if match is None:
        raise ValueError(f"not a Profile field path: {path!r}")
    index = match.group("index")
    return match.group("attrs") + "_exact" + ("" if index is None else f"[{index}]")


def threshold(profile: ValidationProfile, path: str) -> Threshold:
    value = profile_value(profile, path)
    if isinstance(value, Decimal):  # an ExactDecimal field (ADR-0052 §2 / §3)
        return Threshold(value=float(value), source=path, exact=value)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{path!r} is not a numeric Profile field")
    if isinstance(value, float):
        sibling = _exact_sibling_path(path)
        try:
            exact = profile_value(profile, sibling) if profile_has(profile, sibling) else None
        except ProfileFieldMissing:  # the field has no exact sibling in the contract
            exact = None
        if exact is not None:
            if not isinstance(exact, Decimal):  # pragma: no cover - the contract types it
                raise ValueError(f"{sibling!r} is not an exact Profile field")
            return Threshold(value=float(exact), source=sibling, exact=exact)
    return Threshold(value=float(value), source=path)


def _refuse_explicit(path: str, name: str) -> None:
    raise ExplicitParamRefused(
        f"the Profile supplies {path!r}; the explicit parameter "
        f"{PARAM_SOURCE_PREFIX}{name} must not be given as well (C-A4, ADR-0052 §2)"
    )


def sourced_threshold(
    profile: ValidationProfile, path: str, explicit: Threshold | None, name: str
) -> Threshold | None:
    """The rule's threshold: the Profile field ``path`` when the Profile carries it (an explicit
    ``param:<name>`` given as well is refused), else ``explicit`` (``None`` = missing)."""
    if profile_has(profile, path):
        if explicit is not None:
            _refuse_explicit(path, name)
        return threshold(profile, path)
    return explicit


def sourced_parameter[T](
    profile: ValidationProfile, path: str, explicit: T | None, name: str
) -> tuple[T | None, str]:
    """``(value, source)`` of a non-threshold rule parameter (e.g. the CSCV partition count, the
    sealed OOS budget): the Profile field when carried (``source`` = ``path``; an explicit value
    given as well is refused), else the explicit value with source ``param:<name>``."""
    if profile_has(profile, path):
        if explicit is not None:
            _refuse_explicit(path, name)
        return profile_value(profile, path), path  # type: ignore[return-value]
    return explicit, f"{PARAM_SOURCE_PREFIX}{name}"


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
    """PASS / FAIL against a Profile threshold; the Profile band may make it INCONCLUSIVE.

    An exact ``limit`` (ADR-0052 §1) is compared exactly (module docs, **Exact thresholds**).
    """
    if limit.exact is not None:
        return _compare_exact(profile, gate_id, metric, value, limit, limit.exact, direction)
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


def _exact_band(profile: ValidationProfile, gate_id: str) -> Decimal | None:
    """The gate's inconclusive band as an exact number: the exact map when the Profile has one
    (its keys equal the float map's), else the float band's shortest round-trip text."""
    if profile.inconclusive_bands_exact is not None:
        return profile.inconclusive_bands_exact.get(gate_id)
    band = profile.inconclusive_bands.get(gate_id)
    return None if band is None else Decimal(repr(band))


def _compare_exact(
    profile: ValidationProfile,
    gate_id: str,
    metric: str,
    value: float,
    limit: Threshold,
    exact_limit: Decimal,
    direction: Direction,
) -> GateResult:
    value_exact = quantize_gate_value(value)
    if direction is Direction.AT_LEAST:
        passed = value_exact >= exact_limit
    else:
        passed = value_exact <= exact_limit
    verdict = Verdict.PASS if passed else Verdict.FAIL
    band = _exact_band(profile, gate_id)
    if band is not None:
        if band < 0:
            raise ValueError(f"inconclusive band of {gate_id} must be >= 0")
        if abs(value_exact - exact_limit) <= band:
            verdict = Verdict.INCONCLUSIVE
    return GateResult(
        gate_id=gate_id,
        metric=f"{metric}[{direction.value}]",
        value=float(value_exact),
        threshold=float(exact_limit),
        threshold_source=limit.source,
        verdict=verdict,
        value_exact=value_exact,
        threshold_exact=exact_limit,
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
