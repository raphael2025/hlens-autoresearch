"""Explicit one-shot local CLI for the Phase 11 degradation operation (ADR-0067).

Every artifact and output directory is supplied by the caller. This module does not discover
current lifecycle state, select recent reports, aggregate observations, use a clock, or schedule
work. The lifecycle and observation inputs remain caller-declared evidence with the limits
described by ADR-0067.

Run with ``python -m research.operations.degradation_cli --help``. Only one invocation may use a
given reports root at a time; the report writer does not provide a cross-process directory lock.

**Two mutually exclusive input modes (ADR-0098 §4).**

- *caller-declared* (ADR-0067, unchanged): ``--lifecycle`` + ``--recent-manifest``. The report's
  evidence keeps the caller-declared scope texts and has no ``authority`` field; stdout adds
  ``evidence=caller-declared``.
- *authority*: ``--authority-registry <root> --authority-head <hash>|latest --dataset-id <id>
  --manifest-hash <sha256> --authority-as-of <UTC ISO-8601>``. The lifecycle comes from the
  ADR-0098 Lifecycle Registry at that head, opened as a **read-only snapshot**
  (``LifecycleRegistry.open_snapshot``: no writer lock, no anchor crash-recovery write, nothing
  created; the registry must already exist) and truncated to ``--authority-as-of``; the recent
  metrics come from ``research.operations.authority.resolve_degradation_inputs`` as of that time
  (ADR-0098 修订 2: ``as_of`` must not be before ``--window-end``; the window's last bar only
  becomes available after the window ends). stdout adds ``evidence=authority``.
  The resolver also needs a ``DatasetCatalog`` (a live Iceberg catalog, storage and builder), the
  admitted strategy's decision pipeline, the backtest provider and the baseline run / dataset —
  none of which the command line constructs from flags. They come from exactly one of
  (ADR-0098 修订 1):

  - ``--authority-environment MODULE:CALLABLE``: a deployment-supplied **trusted** factory,
    resolved exactly like the ADR-0095 worker ``--factory`` (``apps.worker.serve.load_factory``:
    identifier syntax only, nothing chosen from messages or data). Called with no arguments, it
    returns a ``research.operations.authority.AuthorityEnvironment`` or a context manager
    yielding one (held open until the report is written);
  - ``main(..., authority_environment=...)`` from an embedding caller.

  The default factory (ADR-0100 item 4) is
  ``--authority-environment research.operations.authority_environment:default_environment``: it
  builds the Iceberg catalog, storage and dataset builder from ``infrastructure.settings.Settings``
  and everything the baseline pins (run, strategy spec, cost model, decision grid, v3 evidence
  verifier, optional validation binding) from ``HLENS_AUTHORITY_*`` settings, resolves the backtest
  provider from the plugin registry by the run's recorded identity, and refuses with
  ``authority_environment_unavailable`` naming every missing setting. The environment's optional
  ``validation`` binding is passed to the resolver (needed only by metric definitions that re-run
  the baseline validator on the window).

  Without either, the authority mode refuses (``authority_environment_unavailable``) after
  verifying the pinned head, before any report; a factory that cannot be loaded, fails, or does
  not produce an ``AuthorityEnvironment`` is refused with the same code.

  ``--authority-anchor <path>`` (optional, an existing file outside the registry root) verifies
  the snapshot against its external anchor; any mismatch (including the writer's one-record crash
  window, which only a writer repairs) is refused as ``lifecycle_unavailable``. Without it (also
  with ``--authority-head latest``) the check still runs and the evidence records
  ``authority.lifecycle.anchor = "absent"``; stdout adds ``anchor=...``.

Mixing the two modes, or giving only part of one, is a usage error. Any refusal writes no report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError

from apps.worker.serve import load_factory
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref, exact_decimal, exact_decimal_text
from core.domain.research import ValidationReport
from core.lifecycle.strategy import LifecycleHistory
from infrastructure.registry.lifecycle import JOURNAL_NAME as LIFECYCLE_JOURNAL
from infrastructure.registry.lifecycle import LifecycleHead, LifecycleRegistry, UnknownHead
from infrastructure.registry.profile_freeze import ProfileFreezeRegistry
from infrastructure.registry.registry import RegistryError
from research.operations.degradation import (
    MANIFEST_FORMAT,
    BaselineMetricSet,
    DegradationOperationRefused,
    ObservationSource,
    ObservationWindow,
    RecentMetricManifest,
    RecentMetricSet,
    run_degradation_check,
)
from research.reports.degradation import write_degradation_operation

if TYPE_CHECKING:
    from research.operations.authority import AuthorityEnvironment

#: Refusal code of the authority mode without an embedding caller's environment (module docs).
AUTHORITY_ENVIRONMENT_UNAVAILABLE: Final = "authority_environment_unavailable"
#: ``--authority-head`` value selecting the registry's latest head (read once, then pinned).
LATEST_HEAD: Final = "latest"
_EXPLICIT_OPTIONS: Final = ("lifecycle", "recent_manifest")
_AUTHORITY_OPTIONS: Final = (
    "authority_registry",
    "authority_head",
    "dataset_id",
    "manifest_hash",
    "authority_as_of",
)
#: Optional authority-mode flags (a usage error in the caller-declared mode).
_AUTHORITY_OPTIONAL: Final = ("authority_environment", "authority_anchor")


class _InputError(ValueError):
    """A local input file or field is malformed."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InputError("JSON object has a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise _InputError("JSON contains a non-finite number")


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise _InputError("input is not valid JSON") from exc


def _object(value: Any, label: str, *, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InputError(f"{label} must be a JSON object")
    if keys is not None and set(value) != keys:
        raise _InputError(f"{label} has missing or unknown fields")
    return value


def _utc_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise _InputError(f"{label} must be a UTC ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _InputError(f"{label} must be a UTC ISO-8601 string") from exc
    if result.tzinfo is None or result.utcoffset() != UTC.utcoffset(None):
        raise _InputError(f"{label} must include the UTC offset")
    return result.astimezone(UTC)


def _metric_values(value: Any, label: str) -> dict[str, str]:
    values = _object(value, label)
    result: dict[str, str] = {}
    for key, raw in values.items():
        if not isinstance(key, str) or not isinstance(raw, str):
            raise _InputError(f"{label} keys and values must be strings")
        try:
            decimal = exact_decimal(raw)
        except ValueError as exc:
            raise _InputError(f"{label} contains an invalid exact decimal") from exc
        if exact_decimal_text(decimal) != raw:
            raise _InputError(f"{label} values must use canonical decimal text")
        result[key] = raw
    return result


def _load_model(path: Path, model: type[Any], label: str) -> Any:
    try:
        return model.model_validate(_read_json(path))
    except (ValidationError, ValueError, TypeError) as exc:
        raise _InputError(f"{label} does not match its contract") from exc


def _load_baseline(path: Path, report: ValidationReport) -> BaselineMetricSet:
    payload = _object(
        _read_json(path),
        "baseline set",
        keys={"validation_report_hash", "metrics", "gate_ids"},
    )
    metrics = _metric_values(payload["metrics"], "baseline metrics")
    gates = _object(payload["gate_ids"], "baseline gate_ids")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in gates.items()):
        raise _InputError("baseline gate_ids keys and values must be strings")
    try:
        return BaselineMetricSet(
            validation_report_hash=payload["validation_report_hash"],
            metrics={key: exact_decimal(value) for key, value in metrics.items()},
            gate_ids=gates,
        )
    except (DegradationOperationRefused, TypeError, ValueError) as exc:
        raise _InputError("baseline set is invalid") from exc


def _load_recent(path: Path) -> RecentMetricSet:
    envelope = _object(
        _read_json(path), "recent manifest envelope", keys={"manifest", "manifest_hash"}
    )
    payload = _object(
        envelope["manifest"],
        "recent manifest",
        keys={
            "format",
            "subject",
            "profile_ref",
            "profile_hash",
            "window",
            "observation_set_id",
            "method_id",
            "sources",
            "metrics",
        },
    )
    if payload["format"] != MANIFEST_FORMAT:
        raise _InputError("recent manifest format is unsupported")
    window = _object(payload["window"], "recent manifest window", keys={"start", "end", "label"})
    try:
        observed_window = ObservationWindow(
            start=_utc_datetime(window["start"], "manifest window start"),
            end=_utc_datetime(window["end"], "manifest window end"),
            label=window["label"],
        )
        raw_sources = payload["sources"]
        if not isinstance(raw_sources, list):
            raise _InputError("recent manifest sources must be an array")
        sources: list[ObservationSource] = []
        for raw_source in raw_sources:
            source = _object(
                raw_source,
                "observation source",
                keys={"source_id", "source_hash", "event_time", "observed_time"},
            )
            sources.append(
                ObservationSource(
                    source_id=source["source_id"],
                    source_hash=source["source_hash"],
                    event_time=_utc_datetime(source["event_time"], "source event_time"),
                    observed_time=_utc_datetime(source["observed_time"], "source observed_time"),
                )
            )
        metrics = _metric_values(payload["metrics"], "recent metrics")
        manifest = RecentMetricManifest(
            subject=Ref.parse(payload["subject"]),
            profile_ref=Ref.parse(payload["profile_ref"]),
            profile_hash=payload["profile_hash"],
            window=observed_window,
            observation_set_id=payload["observation_set_id"],
            method_id=payload["method_id"],
            sources=sources,
            metrics={key: exact_decimal(value) for key, value in metrics.items()},
        )
        return RecentMetricSet(manifest=manifest, manifest_hash=envelope["manifest_hash"])
    except (DegradationOperationRefused, TypeError, ValueError) as exc:
        raise _InputError("recent manifest is invalid") from exc


def _existing_registry_paths(root: Path, anchor: Path) -> None:
    """Refuse missing registry material before the registry constructor can create files."""
    if not root.is_dir():
        raise _InputError("freeze registry root must already exist")
    if not (root / ".lock").is_file():
        raise _InputError("freeze registry lock file must already exist")
    if not (root / "freezes.jsonl").is_file():
        raise _InputError("freeze registry journal must already exist")
    if not (root / "blobs").is_dir():
        raise _InputError("freeze registry blob directory must already exist")
    if not anchor.is_file():
        raise _InputError("freeze registry anchor file must already exist")
    if anchor.resolve().is_relative_to(root.resolve()):
        raise _InputError("freeze registry anchor must be outside its root")


def _separate_report_root(registry_root: Path, reports_root: Path) -> None:
    registry = registry_root.resolve()
    reports = reports_root.resolve()
    if registry.is_relative_to(reports) or reports.is_relative_to(registry):
        raise _InputError("reports root and freeze registry must be separate directories")


def _existing_lifecycle_registry(root: Path, others: tuple[Path, ...]) -> None:
    """Refuse a missing Lifecycle Registry (the read-only snapshot needs its journal; nothing is
    ever created) and one that shares a directory with the reports root or the freeze registry."""
    if not root.is_dir():
        raise _InputError("authority registry root must already exist")
    if not (root / LIFECYCLE_JOURNAL).is_file():
        raise _InputError("authority registry journal must already exist")
    resolved = root.resolve()
    for other in others:
        path = other.resolve()
        if resolved.is_relative_to(path) or path.is_relative_to(resolved):
            raise _InputError("the authority registry must not share a directory with other roots")


def _mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """``"explicit"`` or ``"authority"``: exactly one complete input mode (module docs)."""
    explicit = [getattr(args, name) is not None for name in _EXPLICIT_OPTIONS]
    authority = [getattr(args, name) is not None for name in _AUTHORITY_OPTIONS]
    if all(explicit) and not any(authority):
        if any(getattr(args, name) is not None for name in _AUTHORITY_OPTIONAL):
            parser.error("--authority-environment and --authority-anchor need the authority mode")
        return "explicit"
    if all(authority) and not any(explicit):
        return "authority"
    parser.error(
        "give either --lifecycle and --recent-manifest (caller-declared) or all of "
        "--authority-registry, --authority-head, --dataset-id, --manifest-hash and "
        "--authority-as-of (authority)"
    )


def _authority_anchor(anchor: Path | None, root: Path, others: tuple[Path, ...]) -> None:
    """Refuse a missing anchor (the registry would otherwise start a new one) and one inside
    the authority registry or another root."""
    if anchor is None:
        return
    if not anchor.is_file():
        raise _InputError("authority registry anchor file must already exist")
    resolved = anchor.resolve()
    for path in (root, *others):
        if resolved.is_relative_to(path.resolve()):
            raise _InputError("the authority registry anchor must be outside the other roots")


def _factory_environment(spec: str, stack: ExitStack) -> AuthorityEnvironment:
    """Load and call the trusted ``MODULE:CALLABLE`` factory (ADR-0098 修订 1); its context
    manager, if it returns one, is entered on ``stack``. Only exception type names are reported."""
    from research.operations.authority import AuthorityEnvironment, AuthorityRefused

    try:
        factory = cast(Callable[[], object], load_factory(spec))
    except Exception as exc:  # bad syntax, import / attribute failure, not callable
        raise AuthorityRefused(
            AUTHORITY_ENVIRONMENT_UNAVAILABLE,
            f"the authority environment factory could not be loaded ({type(exc).__name__})",
        ) from exc
    try:
        produced = factory()
        if isinstance(produced, AbstractContextManager):
            produced = stack.enter_context(produced)
    except AuthorityRefused:  # a deliberate refusal (e.g. a missing setting) keeps its code
        raise
    except Exception as exc:
        raise AuthorityRefused(
            AUTHORITY_ENVIRONMENT_UNAVAILABLE,
            f"the authority environment factory failed ({type(exc).__name__})",
        ) from exc
    if not isinstance(produced, AuthorityEnvironment):
        raise AuthorityRefused(
            AUTHORITY_ENVIRONMENT_UNAVAILABLE,
            "the authority environment factory did not produce an AuthorityEnvironment",
        )
    return produced


def _pinned_head(registry: LifecycleRegistry, value: str) -> LifecycleHead:
    """The latest head (read once, then used as the pinned head) or the head of a record hash."""
    if value == LATEST_HEAD:
        return registry.head
    return registry.head_for_hash(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicit local P11 degradation check and write its bound report."
    )
    parser.add_argument("--subject", required=True, help="canonical subject Ref, kind:name@version")
    parser.add_argument(
        "--lifecycle", type=Path, help="caller-declared mode: LifecycleHistory JSON"
    )
    parser.add_argument("--profile", required=True, type=Path, help="ValidationProfile JSON")
    parser.add_argument(
        "--baseline-report", required=True, type=Path, help="PASS ValidationReport JSON"
    )
    parser.add_argument(
        "--baseline-set",
        required=True,
        type=Path,
        help="exact baseline metrics and gate-id JSON",
    )
    parser.add_argument(
        "--recent-manifest",
        type=Path,
        help="caller-declared mode: manifest and declared hash JSON",
    )
    parser.add_argument(
        "--authority-registry",
        type=Path,
        help="authority mode: existing ADR-0098 Lifecycle Registry root",
    )
    parser.add_argument(
        "--authority-head",
        help=f"authority mode: pinned head record hash, or {LATEST_HEAD!r}",
    )
    parser.add_argument(
        "--dataset-id", help="authority mode: v3 dataset id (the manifest's selection_id)"
    )
    parser.add_argument(
        "--manifest-hash", help="authority mode: the v3 dataset manifest content hash"
    )
    parser.add_argument(
        "--authority-environment",
        metavar="MODULE:CALLABLE",
        help=(
            "authority mode: trusted deployment factory returning an AuthorityEnvironment or a "
            "context manager yielding one"
        ),
    )
    parser.add_argument(
        "--authority-anchor",
        type=Path,
        help="authority mode, optional: existing external Lifecycle Registry anchor file",
    )
    parser.add_argument(
        "--authority-as-of",
        help=(
            "authority mode: UTC ISO-8601 evaluation time, not before --window-end (data and "
            "lifecycle transitions are used only up to this time)"
        ),
    )
    parser.add_argument("--window-start", required=True, help="UTC ISO-8601 start, inclusive")
    parser.add_argument("--window-end", required=True, help="UTC ISO-8601 end, exclusive")
    parser.add_argument("--window-label", required=True, help="stable report label for this window")
    parser.add_argument(
        "--freeze-registry", required=True, type=Path, help="existing registry root"
    )
    parser.add_argument(
        "--freeze-anchor", required=True, type=Path, help="existing external anchor file"
    )
    parser.add_argument(
        "--reports-root", required=True, type=Path, help="existing report directory"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    authority_environment: AuthorityEnvironment | None = None,
) -> int:
    """Run one P11 check; report only non-sensitive result identity fields.

    ``authority_environment``: for the authority mode only, supplied by an embedding caller
    (module docs); the plain command line uses ``--authority-environment`` instead, never both.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    mode = _mode(parser, args)
    if mode == "explicit" and authority_environment is not None:
        parser.error("an authority environment is only used by the authority mode")
    if authority_environment is not None and args.authority_environment is not None:
        parser.error("give --authority-environment or an embedded environment, not both")
    anchor_state: str | None = None
    stage = "input"
    try:
        subject = Ref.parse(args.subject)
        profile = _load_model(args.profile, ValidationProfile, "profile")
        baseline_report = _load_model(args.baseline_report, ValidationReport, "baseline report")
        baseline = _load_baseline(args.baseline_set, baseline_report)
        window = ObservationWindow(
            start=_utc_datetime(args.window_start, "window start"),
            end=_utc_datetime(args.window_end, "window end"),
            label=args.window_label,
        )
        if not args.reports_root.is_dir():
            raise _InputError("reports root must already exist")
        _existing_registry_paths(args.freeze_registry, args.freeze_anchor)
        _separate_report_root(args.freeze_registry, args.reports_root)
        if mode == "explicit":
            lifecycle = _load_model(args.lifecycle, LifecycleHistory, "lifecycle history")
            recent = _load_recent(args.recent_manifest)

            stage = "operation"
            with ProfileFreezeRegistry(
                args.freeze_registry, anchor=args.freeze_anchor
            ) as freezes:
                result = run_degradation_check(
                    subject=subject,
                    lifecycle=lifecycle,
                    profile=profile,
                    baseline_report=baseline_report,
                    freezes=freezes,
                    baseline=baseline,
                    recent=recent,
                    window=window,
                )
                stage = "report"
                written = write_degradation_operation(args.reports_root, result)
        else:
            _existing_lifecycle_registry(
                args.authority_registry, (args.reports_root, args.freeze_registry)
            )
            _authority_anchor(
                args.authority_anchor,
                args.authority_registry,
                (args.reports_root, args.freeze_registry),
            )
            as_of = _utc_datetime(args.authority_as_of, "authority as-of")
            if as_of < window.end:
                raise _InputError("authority as-of must not be before the window end")
            # imported only in this mode: the resolver pulls in the dataset / catalog stack
            from research.operations.authority import (
                LIFECYCLE_HEAD_UNKNOWN,
                LIFECYCLE_UNAVAILABLE,
                AuthorityEnvironment,
                AuthorityRefused,
                resolve_degradation_inputs,
            )

            stage = "authority"
            with ExitStack() as stack:
                try:  # read-only: no writer lock, no anchor crash-recovery write (修订 2 §6)
                    snapshot = LifecycleRegistry.open_snapshot(
                        args.authority_registry, anchor=args.authority_anchor
                    )
                except RegistryError as exc:
                    raise AuthorityRefused(
                        LIFECYCLE_UNAVAILABLE,
                        f"the Lifecycle Registry snapshot does not verify ({type(exc).__name__})",
                    ) from exc
                registry = stack.enter_context(snapshot)
                freezes = stack.enter_context(
                    ProfileFreezeRegistry(args.freeze_registry, anchor=args.freeze_anchor)
                )
                try:
                    head = _pinned_head(registry, args.authority_head)
                except UnknownHead as exc:
                    raise AuthorityRefused(LIFECYCLE_HEAD_UNKNOWN, str(exc)) from exc
                if args.authority_environment is not None:
                    authority_environment = _factory_environment(
                        args.authority_environment, stack
                    )
                if not isinstance(authority_environment, AuthorityEnvironment):
                    raise AuthorityRefused(
                        AUTHORITY_ENVIRONMENT_UNAVAILABLE,
                        "no --authority-environment factory or embedded environment supplied the "
                        "dataset catalog, decision pipeline and backtest provider",
                    )
                resolution = resolve_degradation_inputs(
                    subject=subject,
                    lifecycle=registry,
                    head=head,
                    profile=profile,
                    baseline_report=baseline_report,
                    baseline=baseline,
                    baseline_run=authority_environment.baseline_run,
                    baseline_manifest_hash=authority_environment.baseline_manifest_hash,
                    catalog=authority_environment.catalog,
                    dataset_id=args.dataset_id,
                    manifest_hash=args.manifest_hash,
                    execution=authority_environment.execution,
                    window=window,
                    as_of=as_of,
                    validation=authority_environment.validation,
                )
                stage = "operation"
                result = run_degradation_check(
                    subject=subject,
                    lifecycle=resolution.lifecycle,
                    profile=profile,
                    baseline_report=baseline_report,
                    freezes=freezes,
                    baseline=baseline,
                    recent=resolution.recent,
                    window=window,
                    authority=resolution.provenance,
                )
                stage = "report"
                written = write_degradation_operation(args.reports_root, result)
                anchor_state = resolution.provenance.lifecycle_anchor

        print(f"status={result.check.status}")
        print(f"check_hash={written.id}")
        print(f"report={written.path}")
        print(f"evidence={'caller-declared' if mode == 'explicit' else 'authority'}")
        if anchor_state is not None:
            print(f"anchor={anchor_state}")
        return 0
    except (OSError, RegistryError) as exc:
        print(
            f"P11 degradation CLI failed at {stage} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 3
    except (
        DegradationOperationRefused,
        _InputError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", None)  # an AuthorityRefused names its refusal code
        detail = type(exc).__name__
        if isinstance(code, str):
            detail = f"{detail}: {code}"
        # the default environment factory names the settings it lacks (names, never values)
        missing = getattr(exc, "missing_settings", ())
        if isinstance(missing, tuple) and missing:
            detail = f"{detail}; missing settings: {', '.join(str(name) for name in missing)}"
        print(f"P11 degradation CLI refused at {stage} ({detail})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
