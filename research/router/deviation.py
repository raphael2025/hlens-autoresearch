"""Paper deviation (Phase 10, P10): the router's net paper result vs a declared reference backtest.

Report only — research code (H5), no threshold, no verdict, nothing here decides anything. It
answers one descriptive question: mark by mark, how far is the router's own paper result
(``RouterPaperRun.result``, net of switching costs) from a ``BacktestResult`` the caller declares
as the reference (e.g. one routed strategy run alone, or a buy-and-hold book over the same bars)?

``paper_deviation(run, reference, *, validation_report, validation_profile,
reference_request=None)``:

- **Refusals** (``DeviationError``, a ``RouterError``): a run whose recorded fields no longer
  match its ``run_hash`` (``RouterPaperRun.verify``); a reference with a different initial
  equity; equity marks that are not exactly the same times, in the same order; a reference that
  filled an instrument the run's request does not price. With ``reference_request`` (optional,
  the request the reference answers) the reference must answer it (``request_hash``) and price
  exactly the run's instruments.
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

Code completion (2026-09-26, CODE_COMPLETE / DEBUG_PENDING).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.strategy import BacktestRequest, BacktestResult
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Kind, content_hash
from core.domain.research import ValidationReport, Verdict
from research.router.paper import MONEY_QUANTUM, RouterPaperRun
from research.router.router import RouterError

__all__ = [
    "PAYLOAD_KIND",
    "RETURN_QUANTUM",
    "DeviationError",
    "DeviationMark",
    "DeviationSummary",
    "PaperDeviation",
    "paper_deviation",
    "validate_scope_bound_payload",
]

#: The payload's ``kind`` (and the report kind ``research/reports/deviation.py`` writes).
PAYLOAD_KIND: Final = "paper_deviation"
SCHEMA_VERSION: Final = "2.0.0"
SCOPE_SCHEMA_VERSION: Final = "1.0.0"
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
NOTE: Final = "descriptive only; no threshold; the reference backtest is declared by the caller"
#: Returns, return differences and their statistics are quantized to this step.
RETURN_QUANTUM: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


class DeviationError(RouterError):
    """The run and the reference cannot be compared mark by mark (module docs, refusals)."""


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
class DeclaredScopeIdentity:
    """P8 profile scope plus the validation identities that declared it."""

    schema_version: str
    validation_profile: str
    validation_profile_hash: str
    validation_report_hash: str
    venue: str
    symbol: str
    timeframe: str
    research_class: str
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_hash", content_hash(self._body()))

    def _body(self) -> dict[str, str]:
        return {
            "scope_schema_version": self.schema_version,
            "validation_profile": self.validation_profile,
            "validation_profile_hash": self.validation_profile_hash,
            "validation_report_hash": self.validation_report_hash,
            "venue": self.venue,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "research_class": self.research_class,
        }

    def to_payload(self) -> dict[str, str]:
        return {**self._body(), "scope_hash": self.scope_hash}


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
    reference_request: BacktestRequest | None,
) -> tuple[tuple[str, ...], DeclaredScopeIdentity]:
    if not isinstance(run, RouterPaperRun) or not isinstance(reference, BacktestResult):
        raise DeviationError("paper_deviation needs a RouterPaperRun and a BacktestResult")
    run.verify()  # a tampered run record is refused (RouterError)
    if not isinstance(validation_report, ValidationReport):
        raise DeviationError("a P8 ValidationReport is required to declare paper deviation scope")
    if not isinstance(validation_profile, ValidationProfile):
        raise DeviationError("the exact ValidationProfile is required to resolve P8 scope")
    try:
        router_name, router_version = run.router.rsplit("@", 1)
    except ValueError as exc:
        raise DeviationError("the router identity is malformed") from exc
    if validation_report.subject.target_identity() != (
        Kind.STRATEGY,
        router_name,
        router_version,
    ):
        raise DeviationError("the P8 ValidationReport is not about this router")
    if (
        validation_report.validation_profile.target_identity()
        != validation_profile.ref.target_identity()
        or validation_report.validation_profile_hash != validation_profile.content_hash()
    ):
        raise DeviationError("the P8 ValidationReport does not bind the supplied ValidationProfile")
    if validation_report.verdict is not Verdict.PASS:
        raise DeviationError("the P8 ValidationReport must PASS to declare deviation scope")
    if not any(gate.gate_id.startswith("G5.") for gate in validation_report.gates):
        raise DeviationError("the P8 ValidationReport must include sealed OOS G5 evidence")
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
            "the run's priced instruments must exactly match the P8 declared scope symbol"
        )
    unpriced = sorted({fill.instrument for fill in reference.fills} - set(instruments))
    if unpriced:
        raise DeviationError(f"the reference trades instruments the run does not price: {unpriced}")
    if reference_request is not None:
        if not isinstance(reference_request, BacktestRequest):
            raise DeviationError("reference_request must be a BacktestRequest")
        if reference.request_hash != reference_request.content_hash():
            raise DeviationError("the reference does not answer reference_request")
        priced = tuple(sorted({bar.instrument for bar in reference_request.bars}))
        if priced != instruments:
            raise DeviationError(
                f"the reference prices {list(priced)}, the paper run {list(instruments)}"
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
    reference_request: BacktestRequest | None = None,
) -> PaperDeviation:
    """Per-mark and summary deviation of ``run.result`` from ``reference`` (module docs)."""
    if validation_report is None or validation_profile is None:
        raise DeviationError(
            "scope-bound paper deviation requires a P8 ValidationReport and ValidationProfile"
        )
    instruments, declared_scope = _check_aligned(
        run, reference, validation_report, validation_profile, reference_request
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


def validate_scope_bound_payload(
    payload: Mapping[str, object],
    *,
    validation_report: ValidationReport,
    validation_profile: ValidationProfile,
) -> None:
    """Fail closed unless a payload is a valid current scope-bound deviation report.

    Historical 1.0.0 reports remain available from the generic report store, but deliberately do
    not pass this evidence validator because they contain no declared-scope identity. The caller
    must resolve and supply the exact P8 report and profile; self-hashes alone are not authority.
    """
    if payload.get("kind") != PAYLOAD_KIND or payload.get("schema_version") != SCHEMA_VERSION:
        raise DeviationError("scope evidence requires paper_deviation schema 2.0.0")
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
        raise DeviationError("the P8 ValidationReport is not about this router")
    if (
        validation_report.validation_profile.target_identity()
        != validation_profile.ref.target_identity()
        or validation_report.validation_profile_hash != validation_profile.content_hash()
    ):
        raise DeviationError("the P8 ValidationReport does not bind the supplied ValidationProfile")
    if validation_report.verdict is not Verdict.PASS or not any(
        gate.gate_id.startswith("G5.") for gate in validation_report.gates
    ):
        raise DeviationError("the P8 ValidationReport must PASS and include sealed OOS G5 evidence")
    scope = payload.get("declared_scope")
    if not isinstance(scope, Mapping) or scope.get("scope_schema_version") != SCOPE_SCHEMA_VERSION:
        raise DeviationError("paper deviation has no supported declared scope")
    scope_hash = scope.get("scope_hash")
    scope_body = {key: value for key, value in scope.items() if key != "scope_hash"}
    if not isinstance(scope_hash, str) or content_hash(scope_body) != scope_hash:
        raise DeviationError("paper deviation declared scope hash does not match")
    expected_scope = DeclaredScopeIdentity(
        schema_version=SCOPE_SCHEMA_VERSION,
        validation_profile=str(validation_profile.ref),
        validation_profile_hash=validation_profile.content_hash(),
        validation_report_hash=validation_report.content_hash(),
        venue=validation_profile.scope.venue,
        symbol=validation_profile.scope.symbol,
        timeframe=validation_profile.scope.timeframe,
        research_class=validation_profile.scope.research_class,
    ).to_payload()
    if dict(scope) != expected_scope:
        raise DeviationError("paper deviation declared scope does not match the P8 evidence")
    instruments = payload.get("instruments")
    if instruments != [scope.get("symbol")]:
        raise DeviationError("paper deviation instruments do not match its declared scope")
    deviation_hash = payload.get("deviation_hash")
    body = {key: value for key, value in payload.items() if key != "deviation_hash"}
    if not isinstance(deviation_hash, str) or content_hash(body) != deviation_hash:
        raise DeviationError("paper deviation hash does not match its payload")


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
