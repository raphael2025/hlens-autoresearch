"""Explicit, one-shot degradation check with bound evidence (Phase 11; ADR-0049, ADR-0067).

``DegradationMonitor`` (``apps/worker/degradation.py``) compares recent metrics with a validation
baseline under the bound Profile's thresholds. Its caller had to resolve every input itself and
nothing recorded where those inputs came from. This module is the composition layer ADR-0067
decides: the caller supplies **every** input explicitly, the operation validates their bindings
once, fail closed, recomputes the check with the monitor and returns an immutable result carrying
the check, the monitor, the exact metric maps and the evidence mapping a report needs.

**Inputs** (all required, none defaulted, nothing discovered): the subject ``Ref``; the subject's
``LifecycleHistory``; the ``ValidationProfile``; the subject's PASS ``ValidationReport`` for that
exact Profile; an open ``ProfileFreezeRegistry``; a ``BaselineMetricSet`` (exact values plus one
``gate_id`` per metric); a ``RecentMetricSet`` (a ``RecentMetricManifest`` and its declared content
hash); an ``ObservationWindow`` (UTC, half-open ``[start, end)``). No clock, network, loop summary,
latest-file search, default window, default threshold or float guess is used.

**Rules** — any failure raises ``DegradationOperationRefused`` and nothing is produced:

1. every object is re-validated through its model (no ``model_construct`` shortcut); the history,
   the report and the manifest name the same subject (``Ref.target_identity``);
2. the supplied history replays to ``ACTIVE``. It is **only** "the history the caller supplied ends
   in ACTIVE": the operation does not claim it is the latest authoritative lifecycle record;
3. the report's verdict is PASS and its Profile ref / hash are exactly the Profile's; the Profile's
   own status is ``FROZEN`` **and** ``freezes.frozen_record(profile)`` returns the ADR-0062 record
   for exactly this ref + hash (an empty, closed or poisoned registry refuses);
4. the monitor is ``DegradationMonitor.from_profile(profile)`` unchanged (exact thresholds first);
   a Profile ruling one metric under two keys is refused as ambiguous;
5. the baseline set cites the report's content hash; its metrics are exactly the Profile's ruled
   metrics; each maps to exactly one gate whose ``metric`` label equals the key and which carries
   ``value_exact`` equal to the supplied value (a float-only gate is refused, never converted);
6. the recent manifest's declared hash equals its recomputed content hash; it binds the same
   subject, Profile ref / hash and window, a method id, and at least one source whose event time
   lies inside the window and whose observed time is no later than the window end;
7. metric keys follow the monitor's grammar (``[A-Za-z0-9_.]+``), are unique, and every value is
   a finite ``ExactDecimal`` (``Decimal`` / ``int`` / canonical text; floats and bools refused).

Missing recent metrics stay missing; the monitor's three statuses are preserved and none of them is
a health claim. The operation holds no bus, publishes no event, writes no report, never touches
lifecycle state and does not aggregate raw observations: the complete caller-declared manifest is
embedded in the report, but the truthfulness of its sources and aggregation is **not** verified
(``RECENT_METRICS_SCOPE``).

**Authority provenance (ADR-0098 §4, additive).** ``run_degradation_check`` also accepts an
optional ``authority`` — the ``AuthorityProvenance`` that ``research.operations.authority
.resolve_degradation_inputs`` produced together with the ``lifecycle`` and ``recent`` inputs. It is
bound (same subject, replayed history hash, recent manifest hash, Profile, report and window) and
written as ``evidence.authority``; the two scope texts then describe the authority path
(``AUTHORITY_LIFECYCLE_SCOPE`` / ``AUTHORITY_RECENT_METRICS_SCOPE``). Without it (the explicit
caller-declared path) nothing changes: no ``authority`` key, the caller-declared scope texts, the
same evidence mapping and report hash as before.

Code completion (2026-09-27, CODE_COMPLETE / DEBUG_PENDING; authority provenance 2026-09-30).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from apps.worker.degradation import DegradationCheck, DegradationMonitor
from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.base import Kind, Ref, content_hash, exact_decimal, exact_decimal_text
from core.domain.research import ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from infrastructure.registry.profile_freeze import ProfileFreeze, ProfileFreezeRegistry
from infrastructure.registry.registry import RegistryError

if TYPE_CHECKING:
    from research.operations.authority import AuthorityProvenance

__all__ = [
    "AUTHORITY_LIFECYCLE_SCOPE",
    "AUTHORITY_RECENT_METRICS_SCOPE",
    "LIFECYCLE_SCOPE",
    "MANIFEST_FORMAT",
    "RECENT_METRICS_SCOPE",
    "BaselineMetricSet",
    "DegradationEvidence",
    "DegradationOperationRefused",
    "DegradationOperationResult",
    "ObservationSource",
    "ObservationWindow",
    "RecentMetricManifest",
    "RecentMetricSet",
    "run_degradation_check",
]

#: Identity of this module's recent-metric manifest payload (part of its content hash).
MANIFEST_FORMAT: Final = "hlens.p11.recent-metric-manifest@1.0.0"
#: What the lifecycle binding does and does not prove (ADR-0067 decision 2).
LIFECYCLE_SCOPE: Final = (
    "the caller-supplied lifecycle history replays to ACTIVE; it is not verified to be the latest "
    "authoritative lifecycle record"
)
#: What the recent-metric binding does and does not prove (ADR-0067 decision 5).
RECENT_METRICS_SCOPE: Final = (
    "the recent metrics are bound by content hash to a caller-declared manifest and method id; the "
    "truthfulness of their sources and aggregation is not verified"
)

#: What the authority-resolved lifecycle binding proves (ADR-0098 §1 / §4).
AUTHORITY_LIFECYCLE_SCOPE: Final = (
    "the lifecycle history is the ADR-0098 Lifecycle Registry's replay of this subject at the head "
    "recorded in authority.lifecycle; the registry's declared actors are not authenticated"
)
#: What the authority-resolved recent metrics prove (ADR-0098 §2 / §3 / §4).
AUTHORITY_RECENT_METRICS_SCOPE: Final = (
    "the recent metrics were computed by the closed ADR-0098 metric definitions (the validation "
    "functions of the baseline gates) from one backtest over the pinned v3 dataset manifest in "
    "authority.source; the decision pipeline is bound by its declared identities, not re-derived"
)

type MetricEntries = Mapping[str, object] | Iterable[tuple[str, object]]
type GateEntries = Mapping[str, str] | Iterable[tuple[str, str]]

#: The metric part of ``apps.worker.degradation._KEY``: the only accepted metric-key grammar.
_METRIC: Final = re.compile(r"^[A-Za-z0-9_.]+$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
#: Caller-supplied opaque identifiers: printable ASCII, no whitespace, bounded.
_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,255}$")


class DegradationOperationRefused(ValueError):
    """An input or binding failed a rule (module docs); nothing was produced."""


# ---- small checks -------------------------------------------------------------------------


def _utc(moment: object, label: str) -> datetime:
    if not isinstance(moment, datetime):
        raise DegradationOperationRefused(f"{label} must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise DegradationOperationRefused(f"{label} must be a timezone-aware UTC datetime")
    return moment.astimezone(UTC)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DegradationOperationRefused(f"{label} must be a lowercase SHA-256 hex string")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DegradationOperationRefused(
            f"{label} must be a non-empty identifier matching {_IDENTIFIER.pattern}"
        )
    return value


def _pairs[V](
    entries: Mapping[str, V] | Iterable[tuple[str, V]], label: str
) -> list[tuple[str, V]]:
    """The entries as pairs with string keys; a repeated key is refused."""
    if isinstance(entries, (str, bytes)):
        raise DegradationOperationRefused(f"{label} must be a mapping or key/value pairs")
    items = list(entries.items()) if isinstance(entries, Mapping) else list(entries)
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise DegradationOperationRefused(f"{label} entries must be (key, value) pairs")
        key = item[0]
        if not isinstance(key, str):
            raise DegradationOperationRefused(f"{label} key {key!r} is not a string")
        if key in seen:
            raise DegradationOperationRefused(f"{label} repeats the key {key!r}")
        seen.add(key)
    return items


def _metrics(entries: MetricEntries, label: str) -> tuple[tuple[str, Decimal], ...]:
    """Unique, grammar-checked metric keys with finite exact values, sorted by key."""
    result: list[tuple[str, Decimal]] = []
    for key, value in _pairs(entries, label):
        if not isinstance(key, str) or _METRIC.fullmatch(key) is None:
            raise DegradationOperationRefused(f"{label} key {key!r} is not a metric name")
        try:
            number = exact_decimal(value)
        except ValueError as exc:
            raise DegradationOperationRefused(f"{label}[{key!r}]: {exc}") from exc
        result.append((key, number))
    return tuple(sorted(result))


def _revalidated[M: (LifecycleHistory, ValidationProfile, ValidationReport)](
    value: object, model: type[M], label: str
) -> M:
    if not isinstance(value, model):
        raise DegradationOperationRefused(f"{label} must be a {model.__name__}")
    try:  # no model_construct shortcut: the full validators run again
        return model.model_validate_json(value.model_dump_json())
    except (ValidationError, ValueError) as exc:
        raise DegradationOperationRefused(f"{label} does not validate: {exc}") from exc


def _same_target(left: Ref, right: Ref) -> bool:
    return left.target_identity() == right.target_identity()


# ---- value objects ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    """The half-open UTC interval ``[start, end)`` the recent metrics describe; ``label`` is the
    stable display name a report shows as its ``window``."""

    start: datetime
    end: datetime
    label: str

    def __post_init__(self) -> None:
        start = _utc(self.start, "window.start")
        end = _utc(self.end, "window.end")
        if end <= start:
            raise DegradationOperationRefused("window.end must be after window.start")
        if not isinstance(self.label, str) or not self.label.strip():
            raise DegradationOperationRefused("window.label must name the window")
        if self.label != self.label.strip():
            raise DegradationOperationRefused("window.label must not have outer whitespace")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def payload(self) -> dict[str, str]:
        return {
            "start": _utc_text(self.start),
            "end": _utc_text(self.end),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class ObservationSource:
    """One external source behind the recent metrics: its id, content hash, event time and the
    time it was observed. Raw rows stay outside the repository; only this identity is bound."""

    source_id: str
    source_hash: str
    event_time: datetime
    observed_time: datetime

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _sha256(self.source_hash, f"source {self.source_id} hash")
        object.__setattr__(self, "event_time", _utc(self.event_time, "source event_time"))
        object.__setattr__(self, "observed_time", _utc(self.observed_time, "source observed_time"))

    def payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "event_time": _utc_text(self.event_time),
            "observed_time": _utc_text(self.observed_time),
        }


@dataclass(frozen=True, slots=True, init=False)
class RecentMetricManifest:
    """The caller-declared recent observation manifest (ADR-0067 decision 5): subject, Profile
    ref / hash, window, set id, aggregation method id, sources and the resulting metric values.
    A ruled metric absent here is *missing*, never healthy."""

    subject: Ref
    profile_ref: Ref
    profile_hash: str
    window: ObservationWindow
    observation_set_id: str
    method_id: str
    sources: tuple[ObservationSource, ...]
    metrics: tuple[tuple[str, Decimal], ...]

    def __init__(
        self,
        *,
        subject: Ref,
        profile_ref: Ref,
        profile_hash: str,
        window: ObservationWindow,
        observation_set_id: str,
        method_id: str,
        sources: Iterable[ObservationSource],
        metrics: MetricEntries,
    ) -> None:
        if not isinstance(subject, Ref) or not isinstance(profile_ref, Ref):
            raise DegradationOperationRefused("manifest subject and profile_ref must be Refs")
        if profile_ref.kind is not Kind.PROFILE:
            raise DegradationOperationRefused("manifest profile_ref must point to a profile")
        if not isinstance(window, ObservationWindow):
            raise DegradationOperationRefused("manifest window must be an ObservationWindow")
        checked = tuple(sources)
        if not checked or not all(isinstance(source, ObservationSource) for source in checked):
            raise DegradationOperationRefused("a manifest needs at least one ObservationSource")
        ids = [source.source_id for source in checked]
        if len(set(ids)) != len(ids):
            raise DegradationOperationRefused("manifest sources repeat a source_id")
        for source in checked:
            if not window.contains(source.event_time):
                raise DegradationOperationRefused(
                    f"source {source.source_id}'s event time is outside the window"
                )
            if source.observed_time > window.end:
                raise DegradationOperationRefused(
                    f"source {source.source_id} was observed after the window ended"
                )
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "profile_ref", profile_ref)
        object.__setattr__(self, "profile_hash", _sha256(profile_hash, "manifest profile_hash"))
        object.__setattr__(self, "window", window)
        object.__setattr__(
            self, "observation_set_id", _identifier(observation_set_id, "observation_set_id")
        )
        object.__setattr__(self, "method_id", _identifier(method_id, "method_id"))
        object.__setattr__(self, "sources", tuple(sorted(checked, key=lambda s: s.source_id)))
        object.__setattr__(self, "metrics", _metrics(metrics, "recent metrics"))

    def payload(self) -> dict[str, Any]:
        """The canonical, JSON-ready manifest payload whose ``content_hash`` is its identity."""
        return {
            "format": MANIFEST_FORMAT,
            "subject": str(self.subject),
            "profile_ref": str(self.profile_ref),
            "profile_hash": self.profile_hash,
            "window": self.window.payload(),
            "observation_set_id": self.observation_set_id,
            "method_id": self.method_id,
            "sources": [source.payload() for source in self.sources],
            "metrics": {key: exact_decimal_text(value) for key, value in self.metrics},
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True, slots=True)
class RecentMetricSet:
    """A manifest and the content hash its caller declares for it (re-checked, never trusted)."""

    manifest: RecentMetricManifest
    manifest_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, RecentMetricManifest):
            raise DegradationOperationRefused("recent needs a RecentMetricManifest")
        _sha256(self.manifest_hash, "recent manifest_hash")


@dataclass(frozen=True, slots=True, init=False)
class BaselineMetricSet:
    """Explicit baseline values and, per metric, the one Validation gate they come from."""

    validation_report_hash: str
    metrics: tuple[tuple[str, Decimal], ...]
    gate_ids: tuple[tuple[str, str], ...]

    def __init__(
        self, *, validation_report_hash: str, metrics: MetricEntries, gate_ids: GateEntries
    ) -> None:
        object.__setattr__(
            self,
            "validation_report_hash",
            _sha256(validation_report_hash, "baseline validation_report_hash"),
        )
        values = _metrics(metrics, "baseline metrics")
        if not values:
            raise DegradationOperationRefused("baseline metrics must not be empty")
        sources: list[tuple[str, str]] = []
        for key, gate_id in _pairs(gate_ids, "baseline gate_ids"):
            if not isinstance(key, str) or _METRIC.fullmatch(key) is None:
                raise DegradationOperationRefused(f"baseline gate_ids key {key!r} is not a metric")
            if not isinstance(gate_id, str) or not gate_id or gate_id != gate_id.strip():
                raise DegradationOperationRefused(f"baseline gate id of {key!r} is not a gate id")
            sources.append((key, gate_id))
        if {key for key, _ in sources} != {key for key, _ in values}:
            raise DegradationOperationRefused(
                "baseline gate_ids must name exactly one gate for every baseline metric"
            )
        if len({gate_id for _, gate_id in sources}) != len(sources):
            raise DegradationOperationRefused("baseline gate_ids map two metrics to one gate")
        object.__setattr__(self, "metrics", values)
        object.__setattr__(self, "gate_ids", tuple(sorted(sources)))


@dataclass(frozen=True, slots=True)
class DegradationEvidence:
    """The ADR-0067 decision 7 evidence identities (all verified bindings, not source truth)."""

    lifecycle_history_hash: str
    profile_ref: str
    profile_hash: str
    profile_freeze_id: str
    profile_freeze_calibration_report_hash: str
    profile_freeze_anchor_length: int
    profile_freeze_anchor_head_hash: str
    validation_report_hash: str
    baseline_gate_ids: tuple[tuple[str, str], ...]
    recent_observation_set_id: str
    recent_observation_set_hash: str
    recent_observation_manifest: RecentMetricManifest
    metric_method_id: str
    window_start: datetime
    window_end: datetime
    #: ADR-0098 provenance; ``None`` on the explicit caller-declared path (no ``authority`` key).
    authority: AuthorityProvenance | None = None

    def as_mapping(self) -> dict[str, Any]:
        """A fresh, JSON-ready mapping for a report's ``evidence`` object."""
        authoritative = self.authority is not None
        mapping: dict[str, Any] = {
            "lifecycle_history_hash": self.lifecycle_history_hash,
            "lifecycle_scope": AUTHORITY_LIFECYCLE_SCOPE if authoritative else LIFECYCLE_SCOPE,
            "profile_ref": self.profile_ref,
            "profile_hash": self.profile_hash,
            "profile_freeze_id": self.profile_freeze_id,
            "profile_freeze_calibration_report_hash": self.profile_freeze_calibration_report_hash,
            "profile_freeze_anchor_length": self.profile_freeze_anchor_length,
            "profile_freeze_anchor_head_hash": self.profile_freeze_anchor_head_hash,
            "validation_report_hash": self.validation_report_hash,
            "baseline_gate_ids": dict(self.baseline_gate_ids),
            "recent_observation_set_id": self.recent_observation_set_id,
            "recent_observation_set_hash": self.recent_observation_set_hash,
            # Inline the complete caller-declared manifest so the report remains independently
            # reviewable after the one-shot process exits. This still does not authenticate its
            # external sources or the caller's aggregation.
            "recent_observation_manifest": self.recent_observation_manifest.payload(),
            "metric_method_id": self.metric_method_id,
            "recent_metrics_scope": (
                AUTHORITY_RECENT_METRICS_SCOPE if authoritative else RECENT_METRICS_SCOPE
            ),
            "window_start": _utc_text(self.window_start),
            "window_end": _utc_text(self.window_end),
        }
        if self.authority is not None:
            mapping["authority"] = self.authority.payload()
        return mapping


_RESULT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class DegradationOperationResult:
    """An operation-validated check and report inputs; constructible only by this module.

    This is an accidental-misuse boundary, not a security signature against hostile Python code.
    """

    check: DegradationCheck
    monitor: DegradationMonitor
    baseline: tuple[tuple[str, Decimal], ...]
    recent: tuple[tuple[str, Decimal], ...]
    window: ObservationWindow
    evidence: DegradationEvidence

    def __init__(
        self,
        *,
        check: DegradationCheck,
        monitor: DegradationMonitor,
        baseline: tuple[tuple[str, Decimal], ...],
        recent: tuple[tuple[str, Decimal], ...],
        window: ObservationWindow,
        evidence: DegradationEvidence,
        _token: object,
    ) -> None:
        if _token is not _RESULT_TOKEN:
            raise TypeError(
                "DegradationOperationResult can only be created by run_degradation_check"
            )
        object.__setattr__(self, "check", check)
        object.__setattr__(self, "monitor", monitor)
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "recent", recent)
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "evidence", evidence)

    def baseline_map(self) -> dict[str, Decimal]:
        return dict(self.baseline)

    def recent_map(self) -> dict[str, Decimal]:
        return dict(self.recent)


# ---- the operation ------------------------------------------------------------------------


def _freeze_record(freezes: object, profile: ValidationProfile) -> ProfileFreeze:
    if not isinstance(freezes, ProfileFreezeRegistry):
        raise DegradationOperationRefused("freezes must be an open ProfileFreezeRegistry")
    try:
        record = freezes.frozen_record(profile)
    except RegistryError as exc:  # closed or poisoned: it cannot answer, so nothing is frozen
        raise DegradationOperationRefused(
            f"the Profile freeze registry cannot answer for {profile.ref}: {exc}"
        ) from exc
    if record is None:
        raise DegradationOperationRefused(
            f"{profile.ref} ({profile.content_hash()}) has no ADR-0062 freeze record; its own "
            "status is not authoritative"
        )
    if record.profile_ref != str(profile.ref) or record.profile_hash != profile.content_hash():
        raise DegradationOperationRefused("the freeze record is not for exactly this Profile")
    return record


def _baseline_values(
    report: ValidationReport, baseline: BaselineMetricSet, ruled: frozenset[str]
) -> tuple[tuple[str, Decimal], ...]:
    if baseline.validation_report_hash != report.content_hash():
        raise DegradationOperationRefused("the baseline set does not cite this validation report")
    supplied = dict(baseline.metrics)
    if set(supplied) != ruled:
        raise DegradationOperationRefused(
            f"baseline metrics {sorted(supplied)} are not exactly the Profile's degradation "
            f"metrics {sorted(ruled)}"
        )
    for metric, gate_id in baseline.gate_ids:
        matches = [gate for gate in report.gates if gate.gate_id == gate_id]
        if len(matches) != 1:
            raise DegradationOperationRefused(
                f"baseline gate {gate_id!r} of {metric!r} matches {len(matches)} gates, not one"
            )
        gate = matches[0]
        if gate.metric != metric:
            raise DegradationOperationRefused(
                f"gate {gate_id!r} reports {gate.metric!r}, not {metric!r}"
            )
        if gate.value_exact is None:
            raise DegradationOperationRefused(
                f"gate {gate_id!r} has no exact value; a float value is never used as a baseline"
            )
        if gate.value_exact != supplied[metric]:
            raise DegradationOperationRefused(
                f"baseline {metric!r} = {exact_decimal_text(supplied[metric])} is not gate "
                f"{gate_id!r}'s exact value {exact_decimal_text(gate.value_exact)}"
            )
    return baseline.metrics


def _check_recent(
    recent: RecentMetricSet,
    *,
    subject: Ref,
    profile: ValidationProfile,
    window: ObservationWindow,
) -> RecentMetricManifest:
    if not isinstance(recent, RecentMetricSet):
        raise DegradationOperationRefused("recent must be a RecentMetricSet")
    manifest = recent.manifest
    if manifest.content_hash() != recent.manifest_hash:
        raise DegradationOperationRefused("the recent manifest's content hash is not the declared")
    if not _same_target(manifest.subject, subject):
        raise DegradationOperationRefused("the recent manifest describes another subject")
    if str(manifest.profile_ref) != str(profile.ref) or manifest.profile_hash != (
        profile.content_hash()
    ):
        raise DegradationOperationRefused("the recent manifest is bound to another Profile")
    if manifest.window != window:
        raise DegradationOperationRefused("the recent manifest describes another window")
    return manifest


def _check_authority(
    authority: object,
    *,
    subject: Ref,
    history: LifecycleHistory,
    profile: ValidationProfile,
    report: ValidationReport,
    recent: RecentMetricSet,
    window: ObservationWindow,
) -> None:
    """The ADR-0098 provenance must describe exactly these inputs (else refused)."""
    # imported here: research.operations.authority imports this module
    from research.operations.authority import AuthorityProvenance

    if not isinstance(authority, AuthorityProvenance):
        raise DegradationOperationRefused("authority must be an AuthorityProvenance")
    if not _same_target(authority.subject, subject):
        raise DegradationOperationRefused("the authority provenance is of another subject")
    if authority.lifecycle_history_hash != history.content_hash():
        raise DegradationOperationRefused(
            "the lifecycle history is not the one the authority replayed"
        )
    if authority.recent_manifest_hash != recent.manifest_hash:
        raise DegradationOperationRefused(
            "the recent manifest is not the one the authority resolved"
        )
    if (authority.profile_ref, authority.profile_hash) != (
        str(profile.ref),
        profile.content_hash(),
    ) or authority.validation_report_hash != report.content_hash():
        raise DegradationOperationRefused(
            "the authority provenance is bound to another Profile or report"
        )
    if (authority.window_start, authority.window_end) != (window.start, window.end):
        raise DegradationOperationRefused("the authority provenance describes another window")


def run_degradation_check(
    *,
    subject: Ref,
    lifecycle: LifecycleHistory,
    profile: ValidationProfile,
    baseline_report: ValidationReport,
    freezes: ProfileFreezeRegistry,
    baseline: BaselineMetricSet,
    recent: RecentMetricSet,
    window: ObservationWindow,
    authority: AuthorityProvenance | None = None,
) -> DegradationOperationResult:
    """Validate every binding (module docs), then recompute the check with the Profile's monitor.

    ``authority`` (optional, ADR-0098): the provenance ``resolve_degradation_inputs`` returned with
    ``lifecycle`` and ``recent``; it must bind exactly these inputs and becomes
    ``evidence.authority``. Raises ``DegradationOperationRefused`` on any failed rule. Writes
    nothing, publishes nothing, never changes lifecycle state.
    """
    if not isinstance(subject, Ref):
        raise DegradationOperationRefused("subject must be a Ref")
    if not isinstance(window, ObservationWindow):
        raise DegradationOperationRefused("window must be an ObservationWindow")
    if not isinstance(baseline, BaselineMetricSet):
        raise DegradationOperationRefused("baseline must be a BaselineMetricSet")
    history = _revalidated(lifecycle, LifecycleHistory, "lifecycle")
    checked_profile = _revalidated(profile, ValidationProfile, "profile")
    report = _revalidated(baseline_report, ValidationReport, "baseline_report")

    if not _same_target(history.subject, subject):
        raise DegradationOperationRefused("the lifecycle history is of another subject")
    if history.current_state is not LifecycleState.ACTIVE:
        raise DegradationOperationRefused(
            f"the supplied lifecycle history ends in {history.current_state}, not ACTIVE"
        )

    if not _same_target(report.subject, subject):
        raise DegradationOperationRefused("the validation report is of another subject")
    if report.verdict is not Verdict.PASS:
        raise DegradationOperationRefused(f"the validation report's verdict is {report.verdict}")
    if str(report.validation_profile) != str(checked_profile.ref) or (
        report.validation_profile_hash != checked_profile.content_hash()
    ):
        raise DegradationOperationRefused("the validation report is bound to another Profile")
    if checked_profile.status is not ProfileStatus.FROZEN:
        raise DegradationOperationRefused(f"{checked_profile.ref} is {checked_profile.status}")
    freeze = _freeze_record(freezes, checked_profile)
    try:
        anchor_length, anchor_head_hash = freezes.anchor_snapshot
    except RegistryError as exc:
        raise DegradationOperationRefused(
            f"the Profile freeze registry anchor cannot be verified: {exc}"
        ) from exc
    if anchor_length < 1 or _SHA256.fullmatch(anchor_head_hash) is None:
        raise DegradationOperationRefused("the Profile freeze registry has no valid anchor head")

    try:
        monitor = DegradationMonitor.from_profile(checked_profile)
    except ValueError as exc:
        raise DegradationOperationRefused(f"the Profile's degradation thresholds: {exc}") from exc
    ruled_list = [rule.metric for rule in monitor.rules]
    ruled = frozenset(ruled_list)
    if len(ruled) != len(ruled_list):
        raise DegradationOperationRefused("the Profile rules one metric under two threshold keys")

    baseline_values = _baseline_values(report, baseline, ruled)
    manifest = _check_recent(recent, subject=subject, profile=checked_profile, window=window)

    try:
        check = monitor.check(subject, dict(baseline_values), dict(manifest.metrics))
    except ValueError as exc:
        raise DegradationOperationRefused(f"the monitor refused the inputs: {exc}") from exc

    if authority is not None:
        _check_authority(
            authority,
            subject=subject,
            history=history,
            profile=checked_profile,
            report=report,
            recent=recent,
            window=window,
        )

    evidence = DegradationEvidence(
        lifecycle_history_hash=history.content_hash(),
        profile_ref=str(checked_profile.ref),
        profile_hash=checked_profile.content_hash(),
        profile_freeze_id=freeze.freeze_id,
        profile_freeze_calibration_report_hash=freeze.report_hash,
        profile_freeze_anchor_length=anchor_length,
        profile_freeze_anchor_head_hash=anchor_head_hash,
        validation_report_hash=report.content_hash(),
        baseline_gate_ids=baseline.gate_ids,
        recent_observation_set_id=manifest.observation_set_id,
        recent_observation_set_hash=recent.manifest_hash,
        recent_observation_manifest=manifest,
        metric_method_id=manifest.method_id,
        window_start=window.start,
        window_end=window.end,
        authority=authority,
    )
    return DegradationOperationResult(
        check=check,
        monitor=monitor,
        baseline=baseline_values,
        recent=manifest.metrics,
        window=window,
        evidence=evidence,
        _token=_RESULT_TOKEN,
    )
