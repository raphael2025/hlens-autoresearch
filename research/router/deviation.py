"""Paper deviation (Phase 10, P10): the router's net paper result vs a declared reference backtest.

Report only — research code (H5), no threshold, no verdict, nothing here decides anything. It
answers one descriptive question: mark by mark, how far is the router's own paper result
(``RouterPaperRun.result``, net of switching costs) from a ``BacktestResult`` the caller declares
as the reference (e.g. one routed strategy run alone, or a buy-and-hold book over the same bars)?

``paper_deviation(run, reference, *, validation_report, validation_profile, router_validation,
reference_request)``:

- **Refusals** (``DeviationError``, a ``RouterError``; ``DeviationError.code`` is one of
  ``DeviationRefusal`` where ADR-0079 / ADR-0104 name the refusal, ``None`` for the plain
  alignment refusals): a run whose recorded fields no longer match its ``run_hash``
  (``RouterPaperRun.verify``); a reference with a different initial equity; equity marks that are
  not exactly the same times, in the same order; a reference that filled an instrument the run's
  request does not price. ``reference_request`` (the request the reference answers) is required:
  the reference must answer it (``request_hash``), price exactly the run's instruments, use
  exactly the run's bars and exactly the run's cost model (ADR-0104: a different cost model is
  always refused, there is no switch).
- **Run binding** (ADR-0104): ``router_validation`` (the ``RouterValidation`` of the router's
  own P8 report) proves through its ``binding_hash`` that the supplied report belongs to the
  run's ``router_spec_hash``; the report's ``experiment_hash``, the bars, the window, the cost
  model and the reference request are recorded in ``RunBinding`` and covered by ``scope_hash``.
- **Per mark** (``DeviationMark``): both equities and ``paper - reference``; both period returns
  (``equity_t / equity_{t-1} - 1``, the initial equity before the first mark; ``None`` when the
  previous equity is not positive) and their difference.
- **Declared scope**: the caller supplies the router's P8 ``ValidationReport`` and its exact
  ``ValidationProfile``. Their subject, profile ref / content hash, PASS verdict and G5 evidence
  are checked; the profile's declared scope is bound into this report. The run and reference
  must price exactly the declared scope's symbol.
- **Summary** (``DeviationSummary``): final / mean / largest absolute equity difference (and when),
  both total returns and their difference, and over the marks where both returns exist the mean
  and mean absolute return difference and the tracking error (sample standard deviation of the
  return differences, ``n - 1``; ``None`` below two such marks).

Arithmetic is ``Decimal`` in a fixed context (50 digits, half-even); returns and statistics are
quantized to ``RETURN_QUANTUM``, money to ``MONEY_QUANTUM``, so the result is deterministic.
``PaperDeviation.to_payload()`` is the JSON-ready form and ``deviation_hash`` the content hash of
everything else in it (``research/reports/deviation.py`` writes it as the ``paper_deviation``
report kind, id = ``deviation_hash``).

Payload 2.1.0 / scope 1.1.0 (ADR-0104) is the only form written. Payload 2.0.0 / scope 1.0.0
(ADR-0079, scope-only, no run binding) stays readable and is reported as ``"scope_only"`` by
``validate_scope_bound_payload``; it is not comparable evidence (``required_min_scope_version``).

Code completion (2026-09-26, CODE_COMPLETE / DEBUG_PENDING); run binding ADR-0104.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final, Literal

from core.contracts.strategy import BacktestRequest, BacktestResult
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Kind, content_hash
from core.domain.research import ValidationReport, Verdict
from research.router.paper import MONEY_QUANTUM, RouterPaperRun
from research.router.router import RouterError
from research.router.validation import RouterValidation, _binding_hash

__all__ = [
    "PAYLOAD_KIND",
    "RETURN_QUANTUM",
    "DeclaredScopeIdentity",
    "DeviationError",
    "DeviationMark",
    "DeviationRefusal",
    "DeviationSummary",
    "PaperDeviation",
    "RunBinding",
    "paper_deviation",
    "validate_scope_bound_payload",
]

#: The payload's ``kind`` (and the report kind ``research/reports/deviation.py`` writes).
PAYLOAD_KIND: Final = "paper_deviation"
SCHEMA_VERSION: Final = "2.1.0"
SCOPE_SCHEMA_VERSION: Final = "1.1.0"
#: ADR-0079 payload / scope versions: scope-only, no run binding; readable, never written.
LEGACY_SCHEMA_VERSION: Final = "2.0.0"
LEGACY_SCOPE_SCHEMA_VERSION: Final = "1.0.0"
SUPPORTED_SCHEMA_VERSIONS: Final = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)
SUPPORTED_SCOPE_VERSIONS: Final = (LEGACY_SCOPE_SCHEMA_VERSION, SCOPE_SCHEMA_VERSION)
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
NOTE: Final = "descriptive only; no threshold; the reference backtest is declared by the caller"
#: Returns, return differences and their statistics are quantized to this step.
RETURN_QUANTUM: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


type DeviationRefusal = Literal[
    "scope_evidence_missing",
    "subject_mismatch",
    "profile_mismatch",
    "verdict_not_pass",
    "sealed_oos_missing",
    "symbol_mismatch",
    "router_spec_mismatch",
    "experiment_mismatch",
    "reference_request_missing",
    "bars_mismatch",
    "cost_model_mismatch",
    "scope_version_unsupported",
]


class DeviationError(RouterError):
    """The run and the reference cannot be compared mark by mark (module docs, refusals).

    ``code`` names the refusal (``DeviationRefusal``); ``None`` for the plain alignment refusals
    (initial equity, equity marks, the reference not answering ``reference_request``, malformed
    inputs, and payload hash checks).
    """

    def __init__(self, message: str, *, code: DeviationRefusal | None = None) -> None:
        super().__init__(message)
        self.code: DeviationRefusal | None = code


@dataclass(frozen=True, slots=True)
class DeviationMark:
    time: datetime
    paper_equity: Decimal
    reference_equity: Decimal
    #: ``paper_equity - reference_equity``
    equity_difference: Decimal
    #: Period return since the previous mark (the initial equity before the first mark);
    #: ``None`` when the previous equity is not positive.
    paper_return: Decimal | None
    reference_return: Decimal | None
    #: ``paper_return - reference_return``; ``None`` when either is ``None``.
    return_difference: Decimal | None


@dataclass(frozen=True, slots=True)
class DeviationSummary:
    marks: int
    initial_equity: Decimal
    final_equity_difference: Decimal
    mean_equity_difference: Decimal
    max_abs_equity_difference: Decimal
    #: The first mark with the largest absolute equity difference.
    max_abs_equity_difference_at: datetime
    paper_total_return: Decimal
    reference_total_return: Decimal
    total_return_difference: Decimal
    #: Marks where both period returns exist (the return statistics are over these).
    return_marks: int
    mean_return_difference: Decimal | None
    mean_abs_return_difference: Decimal | None
    #: Sample standard deviation (``n - 1``) of the return differences; ``None`` below 2 marks.
    tracking_error: Decimal | None


@dataclass(frozen=True, slots=True)
class RunBinding:
    """What a deviation is bound to besides the P8 scope (ADR-0104 §1)."""

    router_spec_hash: str
    router_strategy_spec_hash: str
    #: ``ValidationReport.experiment_hash`` of the supplied P8 report.
    experiment_hash: str
    #: Hash of the run request's bars (the list of their content hashes, canonical bar order).
    bars_hash: str
    #: Earliest ``interval_start`` / latest ``interval_end`` of the run's bars. Recorded only:
    #: the report carries no OOS window to check it against (ADR-0104 §2).
    window_start: datetime
    window_end: datetime
    cost_model_hash: str
    reference_request_hash: str

    def to_payload(self) -> dict[str, str]:
        return {
            "router_spec_hash": self.router_spec_hash,
            "router_strategy_spec_hash": self.router_strategy_spec_hash,
            "experiment_hash": self.experiment_hash,
            "bars_hash": self.bars_hash,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "cost_model_hash": self.cost_model_hash,
            "reference_request_hash": self.reference_request_hash,
        }


def _bars_hash(request: BacktestRequest) -> str:
    return content_hash([bar.content_hash() for bar in request.bars])


@dataclass(frozen=True, slots=True)
class DeclaredScopeIdentity:
    """P8 profile scope plus the validation identities that declared it and the run binding."""

    schema_version: str
    validation_profile: str
    validation_profile_hash: str
    validation_report_hash: str
    venue: str
    symbol: str
    timeframe: str
    research_class: str
    run_binding: RunBinding
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCOPE_SCHEMA_VERSION:
            raise ValueError(f"DeclaredScopeIdentity is scope {SCOPE_SCHEMA_VERSION} (ADR-0104)")
        object.__setattr__(self, "scope_hash", content_hash(self._body()))

    def _body(self) -> dict[str, object]:
        return {
            "scope_schema_version": self.schema_version,
            "validation_profile": self.validation_profile,
            "validation_profile_hash": self.validation_profile_hash,
            "validation_report_hash": self.validation_report_hash,
            "venue": self.venue,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "research_class": self.research_class,
            "run_binding": self.run_binding.to_payload(),
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._body(), "scope_hash": self.scope_hash}


def _scope_base(
    version: str, profile: ValidationProfile, report: ValidationReport
) -> dict[str, str]:
    """The scope fields shared by every scope version (ADR-0079); 1.0.0 is exactly these."""
    return {
        "scope_schema_version": version,
        "validation_profile": str(profile.ref),
        "validation_profile_hash": profile.content_hash(),
        "validation_report_hash": report.content_hash(),
        "venue": profile.scope.venue,
        "symbol": profile.scope.symbol,
        "timeframe": profile.scope.timeframe,
        "research_class": profile.scope.research_class,
    }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class PaperDeviation:
    """The deviation report (module docs); ``deviation_hash`` covers every other field."""

    router: str
    run_hash: str
    #: ``run.result.result_hash``: the router's net paper result that was compared.
    paper_result_hash: str
    reference_result_hash: str
    reference_request_hash: str
    reference_provider: str
    declared_scope: DeclaredScopeIdentity
    #: The instruments the run's request prices (sorted).
    instruments: tuple[str, ...]
    marks: tuple[DeviationMark, ...]
    summary: DeviationSummary
    deviation_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "deviation_hash", content_hash(self._body()))

    def _body(self) -> dict[str, object]:
        summary = self.summary
        return {
            "kind": PAYLOAD_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": STATUS,
            "note": NOTE,
            "router": self.router,
            "run_hash": self.run_hash,
            "paper_result_hash": self.paper_result_hash,
            "reference_result_hash": self.reference_result_hash,
            "reference_request_hash": self.reference_request_hash,
            "reference_provider": self.reference_provider,
            "declared_scope": self.declared_scope.to_payload(),
            "instruments": list(self.instruments),
            "marks": [
                {
                    "time": mark.time.isoformat(),
                    "paper_equity": str(mark.paper_equity),
                    "reference_equity": str(mark.reference_equity),
                    "equity_difference": str(mark.equity_difference),
                    "paper_return": _text(mark.paper_return),
                    "reference_return": _text(mark.reference_return),
                    "return_difference": _text(mark.return_difference),
                }
                for mark in self.marks
            ],
            "summary": {
                "marks": summary.marks,
                "initial_equity": str(summary.initial_equity),
                "final_equity_difference": str(summary.final_equity_difference),
                "mean_equity_difference": str(summary.mean_equity_difference),
                "max_abs_equity_difference": str(summary.max_abs_equity_difference),
                "max_abs_equity_difference_at": summary.max_abs_equity_difference_at.isoformat(),
                "paper_total_return": str(summary.paper_total_return),
                "reference_total_return": str(summary.reference_total_return),
                "total_return_difference": str(summary.total_return_difference),
                "return_marks": summary.return_marks,
                "mean_return_difference": _text(summary.mean_return_difference),
                "mean_abs_return_difference": _text(summary.mean_abs_return_difference),
                "tracking_error": _text(summary.tracking_error),
            },
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._body(), "deviation_hash": self.deviation_hash}


def _period_return(now: Decimal, previous: Decimal) -> Decimal | None:
    if previous <= 0:
        return None
    return (now / previous - 1).quantize(RETURN_QUANTUM)


def _check_aligned(
    run: RouterPaperRun,
    reference: BacktestResult,
    validation_report: ValidationReport,
    validation_profile: ValidationProfile,
    router_validation: RouterValidation,
    reference_request: BacktestRequest | None,
) -> tuple[tuple[str, ...], DeclaredScopeIdentity]:
    if not isinstance(run, RouterPaperRun) or not isinstance(reference, BacktestResult):
        raise DeviationError("paper_deviation needs a RouterPaperRun and a BacktestResult")
    run.verify()  # a tampered run record is refused (RouterError)
    if not isinstance(validation_report, ValidationReport):
        raise DeviationError(
            "a P8 ValidationReport is required to declare paper deviation scope",
            code="scope_evidence_missing",
        )
    if not isinstance(validation_profile, ValidationProfile):
        raise DeviationError(
            "the exact ValidationProfile is required to resolve P8 scope",
            code="scope_evidence_missing",
        )
    if not isinstance(router_validation, RouterValidation):
        raise DeviationError(
            "the router's RouterValidation is required to bind the P8 report to the run",
            code="scope_evidence_missing",
        )
    try:
        router_name, router_version = run.router.rsplit("@", 1)
    except ValueError as exc:
        raise DeviationError("the router identity is malformed") from exc
    if validation_report.subject.target_identity() != (
        Kind.STRATEGY,
        router_name,
        router_version,
    ):
        raise DeviationError(
            "the P8 ValidationReport is not about this router", code="subject_mismatch"
        )
    if (
        validation_report.validation_profile.target_identity()
        != validation_profile.ref.target_identity()
        or validation_report.validation_profile_hash != validation_profile.content_hash()
    ):
        raise DeviationError(
            "the P8 ValidationReport does not bind the supplied ValidationProfile",
            code="profile_mismatch",
        )
    if validation_report.verdict is not Verdict.PASS:
        raise DeviationError(
            "the P8 ValidationReport must PASS to declare deviation scope", code="verdict_not_pass"
        )
    if not any(gate.gate_id.startswith("G5.") for gate in validation_report.gates):
        raise DeviationError(
            "the P8 ValidationReport must include sealed OOS G5 evidence",
            code="sealed_oos_missing",
        )
    bound_report = router_validation.validation.report
    if router_validation.router_spec_hash != run.router_spec_hash:
        raise DeviationError(
            "the RouterValidation is bound to another router spec than the run",
            code="router_spec_mismatch",
        )
    if router_validation.binding_hash != _binding_hash(
        bound_report.content_hash(),
        router_validation.router_spec_hash,
        router_validation.router_strategy_spec_hash,
        router_validation.paper_run_hash,
    ):
        raise DeviationError(
            "the RouterValidation binding_hash does not match its recorded fields",
            code="router_spec_mismatch",
        )
    if bound_report.experiment_hash != validation_report.experiment_hash:
        raise DeviationError(
            "the supplied P8 ValidationReport is of another experiment than the RouterValidation",
            code="experiment_mismatch",
        )
    if bound_report.content_hash() != validation_report.content_hash():
        raise DeviationError(
            "the supplied P8 ValidationReport is not the report the RouterValidation binds",
            code="router_spec_mismatch",
        )
    if reference_request is None:
        raise DeviationError(
            "paper deviation requires the reference_request the reference answers",
            code="reference_request_missing",
        )
    if not isinstance(reference_request, BacktestRequest):
        raise DeviationError("reference_request must be a BacktestRequest")
    paper = run.result
    if reference.initial_equity != paper.initial_equity:
        raise DeviationError(
            f"the reference starts at {reference.initial_equity}, "
            f"the paper run at {paper.initial_equity}"
        )
    paper_times = [point.time for point in paper.equity_curve]
    reference_times = [point.time for point in reference.equity_curve]
    if paper_times != reference_times:
        raise DeviationError("the reference's equity marks are not the paper run's marks")
    instruments = tuple(sorted({bar.instrument for bar in run.request.bars}))
    if instruments != (validation_profile.scope.symbol,):
        raise DeviationError(
            "the run's priced instruments must exactly match the P8 declared scope symbol",
            code="symbol_mismatch",
        )
    unpriced = sorted({fill.instrument for fill in reference.fills} - set(instruments))
    if unpriced:
        raise DeviationError(
            f"the reference trades instruments the run does not price: {unpriced}",
            code="symbol_mismatch",
        )
    if reference.request_hash != reference_request.content_hash():
        raise DeviationError("the reference does not answer reference_request")
    priced = tuple(sorted({bar.instrument for bar in reference_request.bars}))
    if priced != instruments:
        raise DeviationError(
            f"the reference prices {list(priced)}, the paper run {list(instruments)}",
            code="symbol_mismatch",
        )
    if _bars_hash(reference_request) != _bars_hash(run.request):
        raise DeviationError(
            "the reference request prices other bars than the paper run", code="bars_mismatch"
        )
    cost_model_hash = run.request.cost_model.content_hash()
    if reference_request.cost_model.content_hash() != cost_model_hash:
        raise DeviationError(
            "the reference request uses another cost model than the paper run",
            code="cost_model_mismatch",
        )
    declared_scope = DeclaredScopeIdentity(
        schema_version=SCOPE_SCHEMA_VERSION,
        validation_profile=str(validation_profile.ref),
        validation_profile_hash=validation_profile.content_hash(),
        validation_report_hash=validation_report.content_hash(),
        venue=validation_profile.scope.venue,
        symbol=validation_profile.scope.symbol,
        timeframe=validation_profile.scope.timeframe,
        research_class=validation_profile.scope.research_class,
        run_binding=RunBinding(
            router_spec_hash=run.router_spec_hash,
            router_strategy_spec_hash=router_validation.router_strategy_spec_hash,
            experiment_hash=validation_report.experiment_hash,
            bars_hash=_bars_hash(run.request),
            window_start=min(bar.interval_start for bar in run.request.bars),
            window_end=max(bar.interval_end for bar in run.request.bars),
            cost_model_hash=cost_model_hash,
            reference_request_hash=reference_request.content_hash(),
        ),
    )
    return instruments, declared_scope


def _mean(values: Sequence[Decimal], quantum: Decimal) -> Decimal:
    return (sum(values, Decimal(0)) / len(values)).quantize(quantum)


def paper_deviation(
    run: RouterPaperRun,
    reference: BacktestResult,
    *,
    validation_report: ValidationReport | None = None,
    validation_profile: ValidationProfile | None = None,
    router_validation: RouterValidation | None = None,
    reference_request: BacktestRequest | None = None,
) -> PaperDeviation:
    """Per-mark and summary deviation of ``run.result`` from ``reference`` (module docs).

    The four evidence / binding arguments are required (``None`` is refused with the matching
    ``DeviationError.code``); they stay keyword-only with a ``None`` default so a missing one is a
    coded refusal rather than a ``TypeError``.
    """
    if validation_report is None or validation_profile is None or router_validation is None:
        raise DeviationError(
            "scope-bound paper deviation requires a P8 ValidationReport, ValidationProfile "
            "and the router's RouterValidation",
            code="scope_evidence_missing",
        )
    instruments, declared_scope = _check_aligned(
        run, reference, validation_report, validation_profile, router_validation, reference_request
    )
    paper = run.result
    initial = paper.initial_equity
    marks: list[DeviationMark] = []
    with localcontext(_CONTEXT):
        previous_paper = previous_reference = initial
        for ours, theirs in zip(paper.equity_curve, reference.equity_curve, strict=True):
            paper_return = _period_return(ours.equity, previous_paper)
            reference_return = _period_return(theirs.equity, previous_reference)
            marks.append(
                DeviationMark(
                    time=ours.time,
                    paper_equity=ours.equity,
                    reference_equity=theirs.equity,
                    equity_difference=ours.equity - theirs.equity,
                    paper_return=paper_return,
                    reference_return=reference_return,
                    return_difference=None
                    if paper_return is None or reference_return is None
                    else paper_return - reference_return,
                )
            )
            previous_paper, previous_reference = ours.equity, theirs.equity
        summary = _summary(marks, initial, paper.final_equity, reference.final_equity)
    return PaperDeviation(
        router=run.router,
        run_hash=run.run_hash,
        paper_result_hash=paper.result_hash,
        reference_result_hash=reference.result_hash,
        reference_request_hash=reference.request_hash,
        reference_provider=reference.provider,
        declared_scope=declared_scope,
        instruments=instruments,
        marks=tuple(marks),
        summary=summary,
    )


def _semver(text: object) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


_HEX64: Final = frozenset("0123456789abcdef")
_RUN_BINDING_HASHES: Final = (
    "router_spec_hash",
    "router_strategy_spec_hash",
    "experiment_hash",
    "bars_hash",
    "cost_model_hash",
    "reference_request_hash",
)
_RUN_BINDING_KEYS: Final = frozenset((*_RUN_BINDING_HASHES, "window_start", "window_end"))


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _check_run_binding(
    binding: object, payload: Mapping[str, object], validation_report: ValidationReport
) -> None:
    if not isinstance(binding, Mapping) or set(binding) != _RUN_BINDING_KEYS:
        raise DeviationError("paper deviation run binding is malformed")
    if not all(_is_hash(binding[key]) for key in _RUN_BINDING_HASHES):
        raise DeviationError("paper deviation run binding has a malformed hash")
    try:
        start = datetime.fromisoformat(str(binding["window_start"]))
        end = datetime.fromisoformat(str(binding["window_end"]))
        ordered = start <= end
    except (TypeError, ValueError) as exc:
        raise DeviationError("paper deviation run binding window is malformed") from exc
    if not ordered:
        raise DeviationError("paper deviation run binding window ends before it starts")
    if binding["experiment_hash"] != validation_report.experiment_hash:
        raise DeviationError(
            "paper deviation run binding is of another experiment than the P8 report",
            code="experiment_mismatch",
        )
    if binding["reference_request_hash"] != payload.get("reference_request_hash"):
        raise DeviationError(
            "paper deviation run binding does not match its reference request hash"
        )


def validate_scope_bound_payload(
    payload: Mapping[str, object],
    *,
    validation_report: ValidationReport,
    validation_profile: ValidationProfile,
    required_min_scope_version: str = LEGACY_SCOPE_SCHEMA_VERSION,
) -> Literal["run_bound", "scope_only"]:
    """Fail closed unless a payload is a valid scope-bound deviation report.

    Returns ``"run_bound"`` for payload 2.1.0 / scope 1.1.0 (ADR-0104) and ``"scope_only"`` for
    payload 2.0.0 / scope 1.0.0 (ADR-0079: scope identity but no run binding, so not comparable
    evidence). A consumer that makes a comparability judgement passes
    ``required_min_scope_version="1.1.0"``; the default ``1.0.0`` keeps ADR-0079 behaviour. A
    scope of another major version, an unknown minor, or one below the required minimum is
    refused (``scope_version_unsupported``). Payload 1.x reports remain available from the
    generic report store but deliberately do not pass: they contain no declared-scope identity.
    The caller must resolve and supply the exact P8 report and profile; self-hashes alone are not
    authority.
    """
    required = _semver(required_min_scope_version)
    if required is None:
        raise ValueError("required_min_scope_version must be a MAJOR.MINOR.PATCH version")
    payload_version = payload.get("schema_version")
    if payload.get("kind") != PAYLOAD_KIND or payload_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DeviationError(
            "scope evidence requires paper_deviation schema 2.0.0 or 2.1.0",
            code="scope_version_unsupported",
        )
    if not isinstance(validation_report, ValidationReport):
        raise DeviationError("scope evidence needs the P8 ValidationReport")
    if not isinstance(validation_profile, ValidationProfile):
        raise DeviationError("scope evidence needs the exact ValidationProfile")
    router = payload.get("router")
    if not isinstance(router, str) or "@" not in router:
        raise DeviationError("paper deviation has no valid router identity")
    router_name, router_version = router.rsplit("@", 1)
    if validation_report.subject.target_identity() != (
        Kind.STRATEGY,
        router_name,
        router_version,
    ):
        raise DeviationError(
            "the P8 ValidationReport is not about this router", code="subject_mismatch"
        )
    if (
        validation_report.validation_profile.target_identity()
        != validation_profile.ref.target_identity()
        or validation_report.validation_profile_hash != validation_profile.content_hash()
    ):
        raise DeviationError(
            "the P8 ValidationReport does not bind the supplied ValidationProfile",
            code="profile_mismatch",
        )
    if validation_report.verdict is not Verdict.PASS:
        raise DeviationError(
            "the P8 ValidationReport must PASS and include sealed OOS G5 evidence",
            code="verdict_not_pass",
        )
    if not any(gate.gate_id.startswith("G5.") for gate in validation_report.gates):
        raise DeviationError(
            "the P8 ValidationReport must PASS and include sealed OOS G5 evidence",
            code="sealed_oos_missing",
        )
    scope = payload.get("declared_scope")
    scope_version = scope.get("scope_schema_version") if isinstance(scope, Mapping) else None
    parsed = _semver(scope_version)
    if (
        not isinstance(scope, Mapping)
        or parsed is None
        or scope_version not in SUPPORTED_SCOPE_VERSIONS
        or parsed[0] != required[0]
    ):
        raise DeviationError(
            "paper deviation has no supported declared scope", code="scope_version_unsupported"
        )
    if parsed < required:
        raise DeviationError(
            f"paper deviation declared scope {scope_version} is below the required "
            f"{required_min_scope_version}",
            code="scope_version_unsupported",
        )
    if (payload_version, scope_version) not in (
        (LEGACY_SCHEMA_VERSION, LEGACY_SCOPE_SCHEMA_VERSION),
        (SCHEMA_VERSION, SCOPE_SCHEMA_VERSION),
    ):
        raise DeviationError(
            f"paper deviation {payload_version} does not carry scope {scope_version}",
            code="scope_version_unsupported",
        )
    scope_hash = scope.get("scope_hash")
    scope_body = {key: value for key, value in scope.items() if key != "scope_hash"}
    if not isinstance(scope_hash, str) or content_hash(scope_body) != scope_hash:
        raise DeviationError("paper deviation declared scope hash does not match")
    expected_base = _scope_base(str(scope_version), validation_profile, validation_report)
    if scope_version == SCOPE_SCHEMA_VERSION:
        _check_run_binding(scope.get("run_binding"), payload, validation_report)
        mine = {key: value for key, value in scope_body.items() if key != "run_binding"}
    else:
        mine = scope_body
    if mine != expected_base:
        raise DeviationError("paper deviation declared scope does not match the P8 evidence")
    instruments = payload.get("instruments")
    if instruments != [scope.get("symbol")]:
        raise DeviationError("paper deviation instruments do not match its declared scope")
    deviation_hash = payload.get("deviation_hash")
    body = {key: value for key, value in payload.items() if key != "deviation_hash"}
    if not isinstance(deviation_hash, str) or content_hash(body) != deviation_hash:
        raise DeviationError("paper deviation hash does not match its payload")
    return "run_bound" if scope_version == SCOPE_SCHEMA_VERSION else "scope_only"


def _summary(
    marks: Sequence[DeviationMark],
    initial: Decimal,
    paper_final: Decimal,
    reference_final: Decimal,
) -> DeviationSummary:
    """Summary statistics; called inside the module's fixed ``Decimal`` context."""
    differences = [mark.equity_difference for mark in marks]
    widest = max(marks, key=lambda mark: abs(mark.equity_difference))  # first on ties
    paper_total = (paper_final / initial - 1).quantize(RETURN_QUANTUM)
    reference_total = (reference_final / initial - 1).quantize(RETURN_QUANTUM)
    returns = [mark.return_difference for mark in marks if mark.return_difference is not None]
    mean_return = mean_abs = tracking = None
    if returns:
        mean_return = _mean(returns, RETURN_QUANTUM)
        mean_abs = _mean([abs(value) for value in returns], RETURN_QUANTUM)
    if len(returns) >= 2:
        exact_mean = sum(returns, Decimal(0)) / len(returns)
        variance = sum(((value - exact_mean) ** 2 for value in returns), Decimal(0)) / (
            len(returns) - 1
        )
        tracking = variance.sqrt().quantize(RETURN_QUANTUM)
    return DeviationSummary(
        marks=len(marks),
        initial_equity=initial,
        final_equity_difference=paper_final - reference_final,
        mean_equity_difference=_mean(differences, MONEY_QUANTUM),
        max_abs_equity_difference=abs(widest.equity_difference),
        max_abs_equity_difference_at=widest.time,
        paper_total_return=paper_total,
        reference_total_return=reference_total,
        total_return_difference=paper_total - reference_total,
        return_marks=len(returns),
        mean_return_difference=mean_return,
        mean_abs_return_difference=mean_abs,
        tracking_error=tracking,
    )
