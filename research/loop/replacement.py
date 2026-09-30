"""Optional in-loop replacement proposal trigger (ADR-0100 item 7, 2026-09-30; P12-LOOP).

**Default off.** ``LoopWiring.replacement_trigger`` is ``None`` by default, and a
``ReplacementTrigger`` is composed only with the explicit flag ``enabled=True``; without it every
stage, record, fingerprint and state directory is byte-identical to a loop without this module.
There is no scheduler here: the trigger runs inside the loop's own rounds, at the loop's own
cadence, and only in the rounds its ``every_rounds`` names.

**Where it runs.** The worker's stage order is a frozen contract (``core.contracts.loop_audit``:
one optional stage, ``evolution``). The trigger is part of that P12 extension point:
``ReplacementTriggerStage`` wraps the loop's ``EvolutionStage`` under the same stage name, runs it
unchanged, and then — in a due round — runs the trigger. It therefore needs an ``EvolutionPlan``
(the candidates it proposes are evolution offspring) and runs *after* the rounds before it: it sees
everything earlier rounds recorded (lineage, trial ledger, audit) plus this round's ingested data.
A retry round (ADR-0083) never runs it, exactly like evolution. Its audit summary is the
``replacement_trigger`` key of the ``evolution`` stage summary (``REPLACEMENT_TRIGGER_KEY``); the
offspring rows the durable opener compares are untouched.

**Replacement windows are Profile versions (Constitution C-S3; integrity fixes 2026-09-30).** A
sealed boundary is a fixed date defined by a Profile, and rotating it means publishing a new
Profile version. The trigger's windows are therefore given as ``window_profiles``: published
versions of the loop Profile's own family (same ``name``, a strictly newer plain
``major.minor.patch`` version), each ``FROZEN`` with its ADR-0062 record in the caller's
``ProfileFreezeRegistry`` (``frozen_record``: ref + content hash + the cited calibration report),
frozen no later than its window starts. A window is exactly that version's sealed OOS window
(``RegisteredSealedWindow.of``: a reference = Profile ref + content hash + the bounds its
``data_split`` defines). Every window Profile must equal the loop Profile in **every** field except
the sealed-window fields ``data_split.sealed_oos_boundary`` / ``sealed_oos_length`` (identity and
operational metadata — ``name`` / ``version`` / ``created_at`` / ``lineage`` / ``provenance`` /
``status`` — are not thresholds and are not compared): thresholds, costs, splits, scope, budget
and every other rule are identical, so a replacement is never judged by a different rule.

**Budget (Constitution C-S2).** Every window opening is audited (a ledger line and the trigger
row) **and counted toward the same global unsealing budget as the G5 family unsealings**
(``DurableUnsealingLedger.count()`` includes openings). The budget is the one the loop's G5 vault
uses: the loop Profile's ``data_split.sealed_oos_max_unsealings``, else the explicit
``OosUnsealBudget.max_unsealings`` (``trigger_unsealing_budget``); a trigger without either is
refused at composition. An opening when the budget is used up is refused (``refused``, nothing
written); a later G5 unsealing sees every opening.

**What a trigger is.** The caller's ``source(round_index, as_of)`` returns ``ReplacementInputs``
from outside the loop: running incumbents (``Incumbent``: ``ACTIVE`` / ``DEGRADED``), candidates
with the lifecycle a **human** gave them on the Promotion path and the validation reports that back
them, a report resolver and the Profiles of those reports. A candidate is *eligible* when it
descends from a given incumbent in the loop's lineage, is ``PAPER`` / ``PRODUCTION_CANDIDATE``
(OOS → PAPER is a human approval: the loop never makes it, ``apps.worker.loop.FORBIDDEN_TARGETS``)
and has a pair with such an incumbent that the ``ProposalLedger`` does not hold yet. A candidate
goes through **two phases in two different due rounds**, because evidence evaluated on a sealed
window before this loop's ledger opened that window was consumed elsewhere and is never accepted:

*Phase 1 — open* (the candidate was never triggered):

1. **trial** — a ``Hypothesis`` for the candidate version (origin ``combination``, the loop's own
   family: no new family) is registered in the loop's ``TrialLedger`` **before** anything is opened
   or evaluated, so every trigger counts as a trial of the family (G3 sees it) and against the
   ``LoopBudget`` (the stage declares it in ``estimate``). A candidate version is triggered at most
   once, ever: its hypothesis in the ledger is the durable mark;
2. **guard** — see below; a refusal is recorded (``refused``) and nothing is opened;
3. **opening** — the first registered window (configuration order) that passes the guard is
   opened for the candidate: its single ``WindowOpening`` (with ``opened_at`` = the round's
   scheduled time) is journaled in the loop's unsealing ledger (``research.validation.sealed_oos``,
   format 3), counted in the budget. Status ``window_opened``; no evidence is used in this round.

*Phase 2 — evaluate* (a later due round; the ledger holds the candidate's opening and no
consumption of that window):

1. **guard** — the opening must be exactly this candidate's (subject + spec hash, trial + hash,
   this loop, an earlier round, the registered window's registration hash); the window must have
   **ended by the round's ``as_of``**, still be unseen by the loop, not yet consumed, and the
   candidate's ``OOS → PAPER`` approval must still be human. Every claimed report must resolve and
   its Profile must be given. A refusal here consumes nothing (evidence may arrive later);
2. **consumption** — the claimed reports whose Profile's sealed window is the opened window are the
   evidence on it; when there is at least one, the window's single ``WindowConsumption`` (the
   opening hash and those report hashes) is journaled **before** the evidence is checked, so the
   first evidence presented is the only evidence the window ever yields (no shopping among
   several evaluations); a report consumed for another window is refused. With none, the row is
   ``refused`` (awaiting evidence) and nothing is consumed;
3. **evidence** — every claimed report's Profile must equal the loop Profile except the
   sealed-window fields (above); a claimed report on any other window than the loop's own or the
   opened one is refused; every report on the opened window must be of exactly the registered
   Profile version (ref + content hash, still frozen) and bound to **this** ledger's opening: its
   ``created_at`` (the producer's evaluation time; the loop stamps its own reports with its
   scheduled time) must be after the opening's ``opened_at``, not before the window's end and not
   after this round's ``as_of`` — G5 evidence that predates the opening was evaluated elsewhere;
4. **job** — the existing ``research.evolution.replacement_job.propose_replacements`` for that
   candidate and its pending incumbents, with the proposer the loop's own automation identity
   (``apps.worker.loop.loop_actor``), ``proposed_at`` the round's scheduled time and provenance
   evidence appended (``loop:``, ``loop_round:``, ``trial:``, ``sealed_window:``,
   ``sealed_window_opening:``, ``sealed_window_consumption:``). Every claimed report is checked by
   the job (``research.router.evidence.check_report``: found, hash, subject, verdict PASS including
   G5, its Profile given, ADR-0060 items). Proposals go to the caller's durable ``ProposalLedger``
   and are always ``PENDING_HUMAN_APPROVAL``; nothing here approves, promotes, swaps or moves any
   lifecycle state. An exception from the job is recorded as ``failed`` (type and message) and the
   window stays consumed; nothing is retried or deleted.

A window therefore yields **at most one evaluated candidate, ever**: it opens once (for one
subject) and its evidence is consumed once (for that subject).

**The window guard (phase 1).** A trigger may only open a window that is:

- *pre-registered*: the trigger's windows and their freeze records are part of the loop's
  configuration fingerprint, i.e. of the anchored ``loop_state_opened`` header written when the
  state directory was created; reopening with other windows is refused (a new window is a new
  directory), like a budget; and every window must start **strictly after the loop's first
  scheduled ``as_of``** (the loop epoch, round 0) — checked when the header is first written,
  recorded in it (``epoch_check``: the rule, the epoch, every window id and start) and re-checked
  against the recorded values on every reopening;
- *independent*: no registered window overlaps the loop Profile's own sealed OOS window (refused at
  composition: the loop opens that one itself) or another registered window, and a window that
  **any** data the loop has ingested reaches into is refused — the loop has seen it. "Ingested" is
  the union over every recorded round and this one: each recorded round's ingest ``data_window``
  (dataset path), every generated market's whole bar span (synthetic path, ``memory.markets``,
  sealed-window and unused bars included), every accumulated research piece, and this round's
  segment (research span, market, manifests' time ranges). A recorded round whose ingest ran but
  recorded no data window, or an unbound audit (``bind_recorded_rounds``, done by the composition),
  is refused (unknown = seen);
- *never opened*: a window the unsealing ledger records as opened — for anyone, in particular an
  ancestor of the candidate — is never opened again (single use: descendants never reuse a sealed
  window);
- *not evaluated before*: a candidate that already claims a report on any registered window is
  refused (that evaluation happened before this ledger opened the window: consumed elsewhere);
- *within budget*: the global unsealing count is below the budget;
- and the candidate's history holds its ``OOS → PAPER`` transition approved by a non-automation
  identity.

The trigger requires a ``DurableUnsealingLedger`` (a state directory's ``sealed_oos.jsonl``, or
``ResearchMemory(oos_ledger=DurableUnsealingLedger(path))``): an in-memory ledger forgets openings
on a restart and a window could be opened twice.

**Durability** (ADR-0073 / ADR-0083 patterns): the trial registration, the window opening and the
consumption are store writes of the round, inside the state's admission gate, positioned by the
round checkpoint and anchored with it; the audit record holds each trigger's full row (phase,
trial, window, opening, consumption, the job's payload including every proposal); the durable
opener cross-checks every row's trial against the trial ledger and every opening and consumption
against the rows (``research.loop.durable``, cross-check 6). A process that dies inside the round
leaves an unrecorded round, which the opener refuses for human review; a proposal the job had
already recorded stays in the ``ProposalLedger`` (pending, idempotent: the pair is never proposed
twice). The ``ProposalLedger`` and the ``ProfileFreezeRegistry`` live outside the state directory.

**Honest boundary.** A report's ``created_at`` is its producer's declaration; the binding to the
opening is as strong as that declaration (and the report's content hash, which covers it).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from apps.worker.loop import (
    LoopRecord,
    RoundContext,
    StageResult,
    StageStatus,
    StageUsage,
    loop_actor,
)
from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.base import canonical_json, parse_semver
from core.domain.research import Hypothesis, HypothesisOrigin, ValidationReport
from core.lifecycle.strategy import LifecycleState
from infrastructure.registry.profile_freeze import ProfileFreeze, ProfileFreezeRegistry
from research.evolution import LineageGraph
from research.evolution.proposals import (
    CANDIDATE_STATES,
    PENDING_HUMAN_APPROVAL,
    ProposalLedger,
)
from research.evolution.replacement_job import (
    Incumbent,
    ReplacementCandidate,
    propose_replacements,
)
from research.loop.evolution import EvolutionStage, _retry_round
from research.loop.memory import AUTOMATION_ACTOR_PREFIX, ResearchMemory
from research.router.evidence import ReportResolver, ReportUnreadable
from research.validation.gates import sourced_parameter
from research.validation.sealed_oos import (
    MAX_UNSEALINGS_FIELD,
    DurableUnsealingLedger,
    OosBudgetExhausted,
    RegisteredSealedWindow,
    SealedWindow,
    SealedWindowAlreadyConsumed,
    SealedWindowAlreadyOpened,
    WindowConsumption,
    WindowOpening,
)

__all__ = [
    "EPOCH_CHECK_KEY",
    "REPLACEMENT_TRIGGER_KEY",
    "TRIGGER_FORMAT",
    "ReplacementInputs",
    "ReplacementTrigger",
    "ReplacementTriggerStage",
    "check_independent_of_profile",
    "check_windows_after_epoch",
    "epoch_check_payload",
    "profile_differences",
    "trigger_rows",
    "trigger_unsealing_budget",
]

#: The key of the trigger's summary inside the ``evolution`` stage summary.
REPLACEMENT_TRIGGER_KEY: Final = "replacement_trigger"
#: Format of the trigger's summary and fingerprint payload (2: Profile-version windows, the
#: unsealing budget, two phases).
TRIGGER_FORMAT: Final = 2
#: The key of the loop-epoch pre-registration check inside the fingerprinted trigger payload.
EPOCH_CHECK_KEY: Final = "epoch_check"
_EPOCH_RULE: Final = "window.start > loop_epoch (the loop's first scheduled as_of)"
#: Profile fields that are identity or operational metadata, never a rule (module docs).
_PROFILE_METADATA: Final = ("name", "version", "created_at", "lineage", "provenance", "status")
#: The sealed-window fields a newer Profile version may change (C-S3 rotation).
_SEALED_FIELDS: Final = ("sealed_oos_boundary", "sealed_oos_length")
_PHASE_OPEN: Final = "open"
_PHASE_EVALUATE: Final = "evaluate"
_REASON_FIELDS: Final = ("incumbent", "incumbent_state", "candidate", "candidate_state")
_TOKEN: Final = re.compile(r"[^a-z0-9_]")


@dataclass(frozen=True)
class ReplacementInputs:
    """What lives outside the loop, handed in by the caller's source (module docs)."""

    incumbents: Sequence[Incumbent]
    candidates: Sequence[ReplacementCandidate]
    reports: ReportResolver
    profiles: Sequence[ValidationProfile]

    def __post_init__(self) -> None:
        if not all(isinstance(i, Incumbent) for i in self.incumbents):
            raise TypeError("every incumbent must be an Incumbent (spec + ACTIVE/DEGRADED history)")
        if not all(isinstance(c, ReplacementCandidate) for c in self.candidates):
            raise TypeError("every candidate must be a ReplacementCandidate")
        if not all(isinstance(p, ValidationProfile) for p in self.profiles):
            raise TypeError("every profile must be a ValidationProfile")
        if not callable(self.reports):
            raise TypeError("reports must be a resolver: report hash -> ValidationReport | None")


#: ``source(round_index, as_of)``: read-only, deterministic within a round (it is asked once, in
#: the stage's ``estimate``, and the answer is reused by ``run``).
ReplacementSource = Callable[[int, datetime], ReplacementInputs]


def _plain_version(profile: ValidationProfile) -> tuple[int, int, int]:
    """A published version's ``(major, minor, patch)``; prerelease / build versions refused."""
    match = parse_semver(profile.version)
    core = int(match.group("major")), int(match.group("minor")), int(match.group("patch"))
    if profile.version != f"{core[0]}.{core[1]}.{core[2]}":
        raise ValueError(
            f"{profile.ref} is not a published plain major.minor.patch version "
            "(prerelease / build versions are refused)"
        )
    return core


def _rules(profile: ValidationProfile) -> dict[str, Any]:
    """The Profile's rules: everything but metadata and the sealed-window fields (module docs)."""
    data = profile.model_dump(mode="json")
    for name in _PROFILE_METADATA:
        data.pop(name, None)
    split = dict(data["data_split"])
    for name in _SEALED_FIELDS:
        split.pop(name, None)
    data["data_split"] = split
    return data


def profile_differences(profile: ValidationProfile, loop_profile: ValidationProfile) -> list[str]:
    """The rule fields in which ``profile`` differs from ``loop_profile`` (module docs; compared as
    canonical JSON, so ``True`` / ``1`` / ``1.0`` differ). Empty: identical but for the sealed
    window and metadata."""
    mine, theirs = _rules(profile), _rules(loop_profile)
    differing: list[str] = []
    for key in sorted(set(mine) | set(theirs)):
        a, b = mine.get(key), theirs.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            differing.extend(
                f"{key}.{sub}"
                for sub in sorted(set(a) | set(b))
                if canonical_json(a.get(sub)) != canonical_json(b.get(sub))
            )
        elif canonical_json(a) != canonical_json(b):
            differing.append(key)
    return differing


def trigger_unsealing_budget(
    profile: ValidationProfile, explicit: int | None
) -> tuple[int, str]:
    """The global unsealing budget the trigger's openings count against (module docs, **Budget**):
    the loop Profile's field, else the loop's explicit ``OosUnsealBudget.max_unsealings``; neither
    is refused (``ValueError``), both is refused by ``sourced_parameter`` (C-A4)."""
    budget, source = sourced_parameter(profile, MAX_UNSEALINGS_FIELD, explicit, "max_unsealings")
    if budget is None:
        raise ValueError(
            "the replacement trigger's window openings count toward the global unsealing budget "
            f"(C-S2): the loop Profile has no {MAX_UNSEALINGS_FIELD} and the loop declares no "
            "OosUnsealBudget.max_unsealings"
        )
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("the global unsealing budget must be a positive int")
    return budget, source


@dataclass(frozen=True)
class ReplacementTrigger:
    """The trigger's configuration (module docs); no defaults, ``enabled`` must be explicit.

    ``window_profiles`` (their windows and freeze records), ``every_rounds``, ``reason``,
    ``minimum_meaningful_effect`` and ``compute_seconds`` are fingerprinted (``payload``);
    ``source``, ``ledger`` and ``freezes`` are external (code / the caller's stores), like an
    evolution plan's ``provider_for``.
    """

    enabled: bool
    every_rounds: int
    window_profiles: tuple[ValidationProfile, ...]
    freezes: ProfileFreezeRegistry
    source: ReplacementSource
    ledger: ProposalLedger
    reason: str
    minimum_meaningful_effect: str
    compute_seconds: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be an explicit bool")
        if (
            isinstance(self.every_rounds, bool)
            or not isinstance(self.every_rounds, int)
            or self.every_rounds < 1
        ):
            raise ValueError("every_rounds must be a positive int")
        if not isinstance(self.window_profiles, tuple) or not self.window_profiles:
            raise ValueError("a replacement trigger needs a non-empty tuple of window Profiles")
        if not all(isinstance(p, ValidationProfile) for p in self.window_profiles):
            raise TypeError("every window Profile must be a ValidationProfile")
        if not isinstance(self.freezes, ProfileFreezeRegistry):
            raise TypeError("freezes must be an open ProfileFreezeRegistry (ADR-0062)")
        refs = [str(p.ref) for p in self.window_profiles]
        if len(set(refs)) != len(refs):
            raise ValueError(f"window Profile versions must be unique: {refs}")
        if len({p.name for p in self.window_profiles}) != 1:
            raise ValueError("every window Profile must be a version of one Profile family")
        windows = self.windows
        for profile, window in zip(self.window_profiles, windows, strict=True):
            _plain_version(profile)
            freeze = self.freeze_of(profile)
            if freeze.approved_at > window.start:
                raise ValueError(
                    f"{profile.ref} was frozen at {freeze.approved_at.isoformat()}, after its "
                    f"sealed window started ({window.start.isoformat()}): a replacement window "
                    "is published before any of its data exists"
                )
        for index, window in enumerate(windows):
            for other in windows[index + 1 :]:
                if window.overlaps(other.start, other.end):
                    raise ValueError(
                        f"registered windows {window.window_id!r} and {other.window_id!r} "
                        "overlap: independent windows share no instant"
                    )
        if not callable(self.source):
            raise TypeError("source must be callable: (round_index, as_of) -> ReplacementInputs")
        if not isinstance(self.ledger, ProposalLedger):
            raise TypeError("ledger must be a durable ProposalLedger")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a replacement trigger needs a non-empty reason template")
        try:
            self.reason.format(**{name: name for name in _REASON_FIELDS})
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"the reason template may only use {{{'}, {'.join(_REASON_FIELDS)}}}: {exc!r}"
            ) from exc
        if (
            not isinstance(self.minimum_meaningful_effect, str)
            or not self.minimum_meaningful_effect.strip()
        ):
            raise ValueError("minimum_meaningful_effect must be declared (no default)")
        if not isinstance(self.compute_seconds, Decimal) or not self.compute_seconds.is_finite():
            raise TypeError("compute_seconds must be a finite Decimal")
        if self.compute_seconds < 0:
            raise ValueError("compute_seconds must not be negative")

    @property
    def windows(self) -> tuple[RegisteredSealedWindow, ...]:
        """Each window Profile version's sealed window (configuration order)."""
        return tuple(RegisteredSealedWindow.of(profile) for profile in self.window_profiles)

    def freeze_of(self, profile: ValidationProfile) -> ProfileFreeze:
        """The ADR-0062 record freezing exactly ``profile`` (``ValueError`` when there is none)."""
        if profile.status is not ProfileStatus.FROZEN:
            raise ValueError(f"window Profile {profile.ref} is not FROZEN (C-S3: published only)")
        record = self.freezes.frozen_record(profile)
        if record is None:
            raise ValueError(
                f"window Profile {profile.ref} (hash {profile.content_hash()}) has no ADR-0062 "
                "freeze record: only a published, frozen Profile version defines a sealed window"
            )
        return record

    def profile_of(self, window: RegisteredSealedWindow) -> ValidationProfile:
        return next(p for p in self.window_profiles if window.describes(p))

    def payload(self) -> dict[str, Any]:
        """The fingerprinted part (every window's Profile reference and freeze record included)."""
        windows = []
        for profile, window in zip(self.window_profiles, self.windows, strict=True):
            freeze = self.freeze_of(profile)
            windows.append(
                {
                    **window.payload(),
                    "registration_hash": window.registration_hash(),
                    "freeze_id": freeze.freeze_id,
                    "freeze_approved_by": freeze.approved_by,
                    "freeze_approved_at": freeze.approved_at.isoformat(),
                    "calibration_report_hash": freeze.report_hash,
                }
            )
        return {
            "format_version": TRIGGER_FORMAT,
            "enabled": self.enabled,
            "every_rounds": self.every_rounds,
            "windows": windows,
            "reason": self.reason,
            "minimum_meaningful_effect": self.minimum_meaningful_effect,
            "compute_seconds": str(self.compute_seconds),
        }


@dataclass(frozen=True)
class _Eligible:
    candidate: ReplacementCandidate
    hypothesis: Hypothesis
    incumbents: tuple[Incumbent, ...]
    #: phase 2: the candidate's opening in the ledger; ``None``: phase 1
    opening: WindowOpening | None = None


@dataclass(frozen=True)
class _Plan:
    inputs: ReplacementInputs
    eligible: tuple[_Eligible, ...]
    skipped: tuple[dict[str, Any], ...]
    already: tuple[dict[str, Any], ...]


def _token(text: str) -> str:
    return _TOKEN.sub("_", text.lower())


def _human_paper_approval(candidate: ReplacementCandidate) -> str | None:
    """The refusal when the candidate's ``OOS → PAPER`` was not approved by a human."""
    approvals = [
        t
        for t in candidate.history.transitions
        if t.from_state is LifecycleState.OOS and t.to_state is LifecycleState.PAPER
    ]
    if not approvals:
        return "its history holds no OOS → PAPER transition (a human approval, ADR-0006)"
    for transition in approvals:
        approver = transition.approved_by
        if approver is None or not approver.strip() or approver.startswith(AUTOMATION_ACTOR_PREFIX):
            return (
                f"its OOS → PAPER transition was approved by {approver!r}, not a human "
                "(OOS → PAPER requires human approval)"
            )
    return None


def check_independent_of_profile(trigger: ReplacementTrigger, profile: ValidationProfile) -> None:
    """Every window Profile is a newer published version of the loop Profile's family with the
    loop Profile's rules, and no window overlaps the loop Profile's own sealed OOS window
    (``ValueError``; module docs)."""
    own = SealedWindow.from_profile(profile)
    loop_version = _plain_version(profile)
    for window_profile, window in zip(trigger.window_profiles, trigger.windows, strict=True):
        if window_profile.name != profile.name:
            raise ValueError(
                f"window Profile {window_profile.ref} is not a version of the loop Profile "
                f"{profile.name!r} (C-S3: rotation is a new version of the same Profile)"
            )
        if not _plain_version(window_profile) > loop_version:
            raise ValueError(
                f"window Profile {window_profile.ref} is not newer than the loop Profile "
                f"{profile.ref}: the loop's own window is opened by its own G5"
            )
        differing = profile_differences(window_profile, profile)
        if differing:
            raise ValueError(
                f"window Profile {window_profile.ref} differs from the loop Profile in {differing}:"
                " only the sealed-window fields may differ"
            )
        if window.overlaps(own.start, own.end):
            raise ValueError(
                f"registered window {window.window_id!r} overlaps the Profile's sealed OOS "
                f"window [{own.start.isoformat()}, {own.end.isoformat()}): the loop opens "
                "that window itself, so it is not independent"
            )


def check_windows_after_epoch(trigger: ReplacementTrigger, epoch: datetime) -> None:
    """Every registered window starts strictly after the loop's first scheduled ``as_of`` (the
    loop epoch, round 0): a window the loop could already have been running over when it was
    configured is not pre-registered with respect to this loop (``ValueError``)."""
    if not isinstance(epoch, datetime) or epoch.tzinfo is None or epoch.utcoffset() is None:
        raise ValueError("the loop epoch must be a timezone-aware (UTC) datetime")
    for window in trigger.windows:
        if not window.start > epoch:
            raise ValueError(
                f"registered window {window.window_id!r} starts at {window.start.isoformat()}, "
                f"not strictly after the loop's first scheduled as_of {epoch.isoformat()}: a "
                "replacement window must be pre-registered before the loop begins"
            )


def epoch_check_payload(trigger: ReplacementTrigger, epoch: datetime) -> dict[str, Any]:
    """The recorded loop-epoch check (fingerprinted under ``EPOCH_CHECK_KEY``; refuses first)."""
    check_windows_after_epoch(trigger, epoch)
    return {
        "rule": _EPOCH_RULE,
        "loop_epoch": epoch.isoformat(),
        "windows": [
            {"window_id": window.window_id, "start": window.start.isoformat()}
            for window in trigger.windows
        ],
    }


class ReplacementTriggerStage:
    """``EvolutionStage`` followed by the replacement trigger, as the ``evolution`` stage."""

    name = "evolution"

    def __init__(
        self,
        evolution: EvolutionStage,
        memory: ResearchMemory,
        trigger: ReplacementTrigger,
        *,
        loop_id: str,
        family_id: str,
        profile: ValidationProfile,
        loop_epoch: datetime,
        max_unsealings: int,
    ) -> None:
        if not isinstance(trigger, ReplacementTrigger) or trigger.enabled is not True:
            raise ValueError("the replacement trigger is composed only with enabled=True")
        ledger = memory.oos_ledger
        if not isinstance(ledger, DurableUnsealingLedger):
            raise ValueError(
                "the replacement trigger needs a durable unsealing ledger (a state_dir, or "
                "ResearchMemory(oos_ledger=DurableUnsealingLedger(path))): an in-memory ledger "
                "forgets window openings on a restart"
            )
        check_independent_of_profile(trigger, profile)
        check_windows_after_epoch(trigger, loop_epoch)
        if isinstance(max_unsealings, bool) or not isinstance(max_unsealings, int):
            raise ValueError("max_unsealings must be the loop's global unsealing budget (an int)")
        if max_unsealings < 1:
            raise ValueError("max_unsealings must be positive")
        if not family_id.strip():
            raise ValueError("family_id must not be blank")
        self._evolution = evolution
        self._memory = memory
        self._windows_ledger: DurableUnsealingLedger = ledger
        self._trigger = trigger
        self._profile = profile
        self._max_unsealings = max_unsealings
        self._loop_id = loop_id
        self._family_id = family_id
        self._actor = loop_actor(loop_id)
        self._cached: tuple[RoundContext, _Plan] | None = None
        self._rounds: Callable[[], Sequence[LoopRecord]] | None = None

    # ------------------------------------------------------------------ planning

    def _due(self, ctx: RoundContext) -> bool:
        if _retry_round(ctx):  # ADR-0083 "PM 决定" §1: a retry round never evolves or triggers
            return False
        every = self._trigger.every_rounds
        return ctx.round_index > 0 and ctx.round_index % every == 0

    def _hypothesis(self, candidate: ReplacementCandidate) -> Hypothesis:
        spec = candidate.spec
        return Hypothesis(
            name=f"rt_{_token(spec.name)}_v{_token(spec.version)}",
            version="1.0.0",
            family_id=self._family_id,
            statement=(
                f"{spec.ref}, moved to {sorted(s.value for s in CANDIDATE_STATES)} by a human on "
                "the Promotion path, is proposed to replace a running ancestor on evidence "
                "evaluated on an independent pre-registered sealed window; a proposal is never "
                "a promotion"
            ),
            conditions=(
                f"strategy = {spec.name}@{spec.version}",
                f"strategy_hash = {spec.content_hash()}",
            ),
            expected_direction="higher",
            minimum_meaningful_effect=self._trigger.minimum_meaningful_effect,
            origin=HypothesisOrigin.COMBINATION,
            origin_refs=(spec.ref,),
        )

    def _opening_of(self, subject: str) -> WindowOpening | None:
        return next(
            (o for o in self._windows_ledger.window_openings() if o.subject == subject), None
        )

    def _plan(self, ctx: RoundContext) -> _Plan:
        cached = self._cached
        if cached is not None and cached[0] is ctx:
            return cached[1]
        inputs = self._trigger.source(ctx.round_index, ctx.as_of)
        if not isinstance(inputs, ReplacementInputs):
            raise TypeError("the replacement source must return ReplacementInputs")
        lineage = LineageGraph(self._memory.lineage)
        registered = {(h.name, h.version): h for h in self._memory.ledger.hypotheses}
        eligible: list[_Eligible] = []
        skipped: list[dict[str, Any]] = []
        already: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in inputs.candidates:
            ref = str(candidate.spec.ref)
            if ref in seen:
                skipped.append({"candidate": ref, "reason": "listed twice by the source"})
                continue
            seen.add(ref)
            hypothesis = self._hypothesis(candidate)
            known = registered.get((hypothesis.name, hypothesis.version))
            opening: WindowOpening | None = None
            if known is not None:
                if known.content_hash() != hypothesis.content_hash():
                    skipped.append(
                        {
                            "candidate": ref,
                            "reason": f"{known.ref} is registered with other content",
                        }
                    )
                    continue
                opening = self._opening_of(ref)
                if opening is None or (
                    self._windows_ledger.window_consumption(opening.window_id) is not None
                ):
                    # triggered before: refused / failed before any opening, or already evaluated
                    already.append({"candidate": ref, "hypothesis": str(known.ref)})
                    continue
            ancestors = {str(a) for a in lineage.ancestors(candidate.spec.ref)}
            targets = [i for i in inputs.incumbents if str(i.spec.ref) in ancestors]
            if not targets:
                skipped.append({"candidate": ref, "reason": "descends from no given incumbent"})
                continue
            proposals = self._trigger.ledger
            pending = tuple(
                i for i in targets if proposals.proposal_for(str(i.spec.ref), ref) is None
            )
            if not pending:
                skipped.append(
                    {"candidate": ref, "reason": "already proposed for every given incumbent"}
                )
                continue
            state = candidate.history.current_state
            if state not in CANDIDATE_STATES:
                skipped.append(
                    {
                        "candidate": ref,
                        "reason": f"{state.value}: awaiting the human OOS → PAPER approval",
                    }
                )
                continue
            eligible.append(_Eligible(candidate, hypothesis, pending, opening))
        plan = _Plan(inputs, tuple(eligible), tuple(skipped), tuple(already))
        self._cached = (ctx, plan)
        return plan

    # ------------------------------------------------------------------ what the loop has seen

    def bind_recorded_rounds(self, rounds: Callable[[], Sequence[LoopRecord]]) -> None:
        """The loop's recorded rounds (its audit), read by the guard (module docs); once."""
        if self._rounds is not None:
            raise ValueError("the recorded rounds are already bound")
        if not callable(rounds):
            raise TypeError("rounds must be callable: () -> the loop's recorded rounds")
        self._rounds = rounds

    def _seen_spans(self, ctx: RoundContext) -> tuple[list[tuple[datetime, datetime]], str | None]:
        """Every data window the loop has ingested so far — the union over every recorded round
        and this one (module docs, *independent*); ``(spans, None)`` or ``([], refusal)``."""
        if self._rounds is None:
            return [], "the loop's recorded rounds are not bound: the data it has seen is unknown"
        spans: list[tuple[datetime, datetime]] = []

        def add(start: object, end: object) -> bool:
            if isinstance(start, datetime) and isinstance(end, datetime):
                spans.append((start, end))
                return True
            return False

        memory = self._memory
        by_hash: dict[str, tuple[datetime, datetime]] = {}
        for market in memory.markets:  # every generated market the loop held, whole
            bars = getattr(market, "bars", ())
            if bars:
                span = (bars[0].interval_start, bars[-1].interval_end)
                by_hash[str(getattr(market, "market_hash", ""))] = span
                spans.append(span)
        for piece in memory.research_data:
            add(piece.bars[0].interval_start, piece.bars[-1].interval_end)
        for record in self._rounds():
            stage = next((st for st in record.stages if st.name == "ingest"), None)
            if stage is None or stage.status in (StageStatus.SKIPPED, StageStatus.REFUSED_BUDGET):
                continue  # the ingest did not run: it read nothing
            summary = stage.summary
            window = None if summary is None else summary.get("data_window")
            if isinstance(window, Sequence) and not isinstance(window, str) and len(window) == 2:
                try:
                    add(datetime.fromisoformat(window[0]), datetime.fromisoformat(window[1]))
                except (TypeError, ValueError):
                    return [], f"round {record.round_index}'s recorded data window is unreadable"
                continue
            market_hash = None if summary is None else summary.get("market_hash")
            if isinstance(market_hash, str) and market_hash in by_hash:
                continue  # that market's whole span is already in the union
            return [], (
                f"round {record.round_index}'s ingest recorded no data window: the data the loop "
                "has seen is unknown"
            )
        segment = ctx.artifacts.get("ingest", {}).get("segment")
        if segment is None:
            return [], "the round's research data span is unknown (no ingest segment)"
        found = add(
            getattr(segment, "research_start", None), getattr(segment, "research_end", None)
        )
        market = getattr(segment, "market", None)
        bars = getattr(market, "bars", ())
        if bars:
            found = add(bars[0].interval_start, bars[-1].interval_end) or found
        for name in ("price_manifest", "feature_manifest"):
            dataset = getattr(getattr(segment, name, None), "dataset", None)
            found = (
                add(
                    getattr(dataset, "time_range_start", None),
                    getattr(dataset, "time_range_end", None),
                )
                or found
            )
        if not found:
            return [], "the round's ingested data window is unknown"
        return spans, None

    def _unseen(self, ctx: RoundContext, window: RegisteredSealedWindow) -> str | None:
        """``None`` when no data the loop has ingested reaches into ``window``; else why."""
        spans, unknown = self._seen_spans(ctx)
        if unknown is not None:
            return unknown
        for start, end in spans:
            if window.overlaps(start, end):
                return (
                    f"data the loop has ingested [{start.isoformat()}, {end.isoformat()}] "
                    f"reaches into window {window.window_id!r}: the loop has seen it"
                )
        return None

    # ------------------------------------------------------------------ evidence

    def _claimed(
        self, candidate: ReplacementCandidate, inputs: ReplacementInputs
    ) -> tuple[list[tuple[str, ValidationReport, ValidationProfile]], str | None]:
        """Every claimed report with its given Profile, or a refusal (unresolvable = refused)."""
        claimed: list[tuple[str, ValidationReport, ValidationProfile]] = []
        for report_hash in candidate.report_hashes:
            try:
                report = inputs.reports(report_hash)
            except ReportUnreadable as exc:
                return [], f"claimed report {report_hash} is unreadable: {exc}"
            if not isinstance(report, ValidationReport):
                return [], f"claimed report {report_hash} does not resolve to a ValidationReport"
            if report.content_hash() != report_hash:
                return [], f"claimed report {report_hash} does not have that content hash"
            profile = next(
                (
                    p
                    for p in inputs.profiles
                    if p.content_hash() == report.validation_profile_hash
                    and p.ref.target_identity() == report.validation_profile.target_identity()
                ),
                None,
            )
            if profile is None:
                return [], f"the Profile of claimed report {report_hash} is not given"
            claimed.append((report_hash, report, profile))
        return claimed, None

    # ------------------------------------------------------------------ phase 1: open

    def _guard_open(
        self, ctx: RoundContext, item: _Eligible, inputs: ReplacementInputs
    ) -> tuple[RegisteredSealedWindow | None, str | None]:
        """``(window, None)`` or ``(None, refusal)`` (module docs, **The window guard**)."""
        candidate = item.candidate
        refusal = _human_paper_approval(candidate)
        if refusal is not None:
            return None, refusal
        count = self._windows_ledger.count()
        if count >= self._max_unsealings:
            return None, (
                f"the global unsealing budget ({self._max_unsealings}) is used up: {count} "
                "unsealings and window openings are recorded (C-S2)"
            )
        claimed, refusal = self._claimed(candidate, inputs)
        if refusal is not None:
            return None, refusal
        for report_hash, _, profile in claimed:
            sealed = SealedWindow.from_profile(profile)
            for window in self._trigger.windows:
                if window.same_bounds(sealed):
                    return None, (
                        f"claimed report {report_hash} was evaluated on window "
                        f"{window.window_id!r} before this loop's ledger opened it: that "
                        "evaluation was consumed elsewhere"
                    )
        reasons: list[str] = []
        for window in self._trigger.windows:
            opening = self._windows_ledger.window_opening(window.window_id)
            if opening is not None:
                reasons.append(f"{window.window_id}: already opened for {opening.subject}")
                continue
            unseen = self._unseen(ctx, window)
            if unseen is not None:
                reasons.append(f"{window.window_id}: {unseen}")
                continue
            return window, None
        return None, f"no registered window can be opened: {reasons}"

    # ------------------------------------------------------------------ phase 2: evaluate

    def _guard_evaluate(
        self, ctx: RoundContext, item: _Eligible
    ) -> tuple[RegisteredSealedWindow | None, str | None]:
        """The pre-evidence checks of phase 2 (module docs); ``(window, None)`` or a refusal."""
        opening, candidate, hypothesis = item.opening, item.candidate, item.hypothesis
        assert opening is not None
        spec = candidate.spec
        window = next(
            (w for w in self._trigger.windows if w.window_id == opening.window_id), None
        )
        if window is None:
            return None, f"the opened window {opening.window_id!r} is not a registered window"
        if (
            opening.subject != str(spec.ref)
            or opening.subject_hash != spec.content_hash()
            or opening.trial != str(hypothesis.ref)
            or opening.trial_hash != hypothesis.content_hash()
            or opening.loop_id != ctx.loop_id
            or opening.registration_hash != window.registration_hash()
            or not opening.round_index < ctx.round_index
        ):
            return None, f"the opening of {window.window_id!r} is not this candidate's"
        if self._windows_ledger.window_consumption(window.window_id) is not None:
            return None, f"window {window.window_id!r} was already consumed"
        refusal = _human_paper_approval(candidate)
        if refusal is not None:
            return None, refusal
        if not window.end <= ctx.as_of:
            return None, (
                f"window {window.window_id!r} ends at {window.end.isoformat()}, after this "
                f"round's as_of {ctx.as_of.isoformat()}: its evidence cannot exist yet"
            )
        unseen = self._unseen(ctx, window)
        if unseen is not None:
            return None, unseen
        return window, None

    def _evidence(
        self,
        ctx: RoundContext,
        opening: WindowOpening,
        window: RegisteredSealedWindow,
        claimed: Sequence[tuple[str, ValidationReport, ValidationProfile]],
    ) -> str | None:
        """The evidence checks after the consumption (module docs, phase 2 step 3)."""
        own = SealedWindow.from_profile(self._profile)
        registered = self._trigger.profile_of(window)
        for report_hash, report, profile in claimed:
            differing = profile_differences(profile, self._profile)
            if differing:
                return (
                    f"the Profile {profile.ref} of claimed report {report_hash} differs from "
                    f"the loop Profile in {differing}: only the sealed window may differ"
                )
            sealed = SealedWindow.from_profile(profile)
            if (sealed.start, sealed.end) == (own.start, own.end):
                continue  # the loop's own window: supporting, never the replacement evidence
            if not window.same_bounds(sealed):
                return (
                    f"claimed report {report_hash} was evaluated on another sealed window "
                    f"[{sealed.start.isoformat()}, {sealed.end.isoformat()}): not the opened one"
                )
            if not window.describes(profile):
                return (
                    f"claimed report {report_hash} was evaluated under {profile.ref}, not the "
                    f"registered version {window.profile_ref}#{window.profile_hash}"
                )
            try:
                self._trigger.freeze_of(registered)
            except ValueError as exc:
                return str(exc)
            created = report.created_at
            if not opening.opened_at < created:
                return (
                    f"claimed report {report_hash} was created at {created.isoformat()}, not "
                    f"after this ledger opened window {window.window_id!r} "
                    f"({opening.opened_at.isoformat()}): its G5 was evaluated elsewhere"
                )
            if created < window.end or created > ctx.as_of:
                return (
                    f"claimed report {report_hash} was created at {created.isoformat()}, "
                    f"outside [{window.end.isoformat()}, {ctx.as_of.isoformat()}]"
                )
        return None

    # ------------------------------------------------------------------ stage protocol

    def estimate(self, ctx: RoundContext) -> StageUsage:
        usage = self._evolution.estimate(ctx)
        if not self._due(ctx):
            return usage
        plan = self._plan(ctx)
        opening = sum(1 for item in plan.eligible if item.opening is None)
        return usage + StageUsage(trials=opening, compute_seconds=self._trigger.compute_seconds)

    def run(self, ctx: RoundContext) -> StageResult:
        result = self._evolution.run(ctx)
        if not self._due(ctx):
            summary = {**result.summary, REPLACEMENT_TRIGGER_KEY: {"due": False}}
            return StageResult(summary, result.usage, result.artifacts)
        plan = self._plan(ctx)
        rows = [
            self._open_one(ctx, item, plan.inputs)
            if item.opening is None
            else self._evaluate_one(ctx, item, plan.inputs)
            for item in plan.eligible
        ]
        trigger_summary = {
            "due": True,
            "format_version": TRIGGER_FORMAT,
            "status": PENDING_HUMAN_APPROVAL,
            "proposed_by": self._actor,
            "triggers": rows,
            "not_eligible": list(plan.skipped),
            "already_triggered": list(plan.already),
            "family_trials": self._memory.ledger.trials(self._family_id),
            "unsealing_count": self._windows_ledger.count(),
            "unsealing_budget": self._max_unsealings,
        }
        trials = sum(1 for row in rows if row["phase"] == _PHASE_OPEN)
        usage = result.usage + StageUsage(
            trials=trials, compute_seconds=self._trigger.compute_seconds
        )
        return StageResult(
            {**result.summary, REPLACEMENT_TRIGGER_KEY: trigger_summary}, usage, result.artifacts
        )

    def _row(self, item: _Eligible, phase: str) -> dict[str, Any]:
        spec = item.candidate.spec
        return {
            "phase": phase,
            "candidate": str(spec.ref),
            "candidate_hash": spec.content_hash(),
            "candidate_state": item.candidate.history.current_state.value,
            "incumbents": [str(i.spec.ref) for i in item.incumbents],
            "report_hashes": list(item.candidate.report_hashes),
            "trial": {
                "hypothesis": str(item.hypothesis.ref),
                "hypothesis_hash": item.hypothesis.content_hash(),
                "family_id": item.hypothesis.family_id,
            },
            "window": None,
            "window_opening": None,
            "window_opening_hash": None,
            "window_consumption": None,
            "window_consumption_hash": None,
            "job": None,
            "error": None,
            "refusal": None,
        }

    def _open_one(
        self, ctx: RoundContext, item: _Eligible, inputs: ReplacementInputs
    ) -> dict[str, Any]:
        candidate, hypothesis = item.candidate, item.hypothesis
        spec = candidate.spec
        row = self._row(item, _PHASE_OPEN)
        self._memory.ledger.register(hypothesis)  # 1. the trial, before anything is opened
        window, refusal = self._guard_open(ctx, item, inputs)  # 2. the guard
        if window is None:
            row.update(status="refused", refusal=refusal)
            return row
        opening = WindowOpening(
            window_id=window.window_id,
            registration_hash=window.registration_hash(),
            subject=str(spec.ref),
            subject_hash=spec.content_hash(),
            trial=str(hypothesis.ref),
            trial_hash=hypothesis.content_hash(),
            loop_id=ctx.loop_id,
            round_index=ctx.round_index,
            opened_at=ctx.as_of,
        )
        try:  # 3. the single opening, counted in the global unsealing budget (C-S2)
            self._windows_ledger.open_window(opening, max_unsealings=self._max_unsealings)
        except (OosBudgetExhausted, SealedWindowAlreadyOpened) as exc:  # nothing was written
            row.update(status="refused", refusal=str(exc))
            return row
        row.update(
            status="window_opened",
            window={**window.payload(), "registration_hash": window.registration_hash()},
            window_opening=opening.payload(),
            window_opening_hash=opening.opening_hash(),
        )
        return row

    def _evaluate_one(
        self, ctx: RoundContext, item: _Eligible, inputs: ReplacementInputs
    ) -> dict[str, Any]:
        candidate, hypothesis, opening = item.candidate, item.hypothesis, item.opening
        assert opening is not None
        spec = candidate.spec
        row = self._row(item, _PHASE_EVALUATE)
        row.update(window_opening=opening.payload(), window_opening_hash=opening.opening_hash())
        window, refusal = self._guard_evaluate(ctx, item)  # 1. pre-evidence guard
        if window is None:
            row.update(status="refused", refusal=refusal)
            return row
        row["window"] = {**window.payload(), "registration_hash": window.registration_hash()}
        claimed, refusal = self._claimed(candidate, inputs)
        if refusal is not None:
            row.update(status="refused", refusal=refusal)
            return row
        on_window = sorted(
            {
                report_hash
                for report_hash, _, profile in claimed
                if window.same_bounds(SealedWindow.from_profile(profile))
            }
        )
        if not on_window:
            row.update(
                status="refused",
                refusal=f"no claimed report was evaluated on the opened window "
                f"{window.window_id!r} yet (nothing consumed)",
            )
            return row
        consumption = WindowConsumption(
            window_id=window.window_id,
            opening_hash=opening.opening_hash(),
            subject=str(spec.ref),
            report_hashes=tuple(on_window),
            round_index=ctx.round_index,
        )
        try:  # 2. the single consumption, before the evidence is checked or used
            self._windows_ledger.consume_window(consumption)
        except (SealedWindowAlreadyConsumed, ValueError) as exc:  # nothing was written
            row.update(status="refused", refusal=str(exc))
            return row
        row.update(
            window_consumption=consumption.payload(),
            window_consumption_hash=consumption.consumption_hash(),
        )
        refusal = self._evidence(ctx, opening, window, claimed)  # 3. the evidence
        if refusal is not None:
            row.update(status="refused", refusal=refusal)
            return row
        provenance = (
            f"loop:{ctx.loop_id}",
            f"loop_round:{ctx.round_index}",
            f"trial:{hypothesis.ref}#{hypothesis.content_hash()}",
            f"sealed_window:{window.window_id}#{window.registration_hash()}",
            f"sealed_window_opening:{opening.opening_hash()}",
            f"sealed_window_consumption:{consumption.consumption_hash()}",
        )
        try:  # 4. the existing job; any failure is recorded, the window stays consumed
            job = propose_replacements(
                incumbents=item.incumbents,
                candidates=(candidate,),
                reports=inputs.reports,
                profiles=inputs.profiles,
                lineage=LineageGraph(self._memory.lineage),
                ledger=self._trigger.ledger,
                reason=self._trigger.reason,
                proposed_by=self._actor,
                proposed_at=ctx.as_of,
                extra_evidence=provenance,
            )
        except Exception as exc:  # noqa: BLE001 - a failed trigger is research data, kept
            row.update(
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            return row
        row.update(status="proposed" if job.recorded else "not_proposed", job=job.payload())
        return row


def trigger_rows(summary: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """The trigger rows of one ``evolution`` stage summary (empty when absent or not due)."""
    if summary is None:
        return ()
    trigger = summary.get(REPLACEMENT_TRIGGER_KEY)
    if not isinstance(trigger, Mapping) or trigger.get("due") is not True:
        return ()
    rows = trigger.get("triggers", ())
    return tuple(rows)
