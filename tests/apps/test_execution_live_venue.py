"""ADR-0084 smoke tests: the reserved live-trading interface is declared and always refused.

Nothing here talks to a real venue, reads a credential, or performs network I/O — there is nothing
behind ``UnconfiguredLiveVenue`` to connect to. These tests only prove: every port method refuses
and leaves an audit trail; the adapter registry only accepts classes that structurally implement
``LiveVenuePort``; the ``LIVE_TRADING_ENABLED`` build switch cannot be overridden by a parameter,
config or environment variable; ``ExecutionLadder.request_live`` still refuses every live rung and
now cites ADR-0084; and ``apps/execution`` still imports no network / exchange SDK.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.execution import (
    LIVE_TRADING_ENABLED,
    CredentialProvider,
    ExecutionLadder,
    ExecutionStage,
    FillRecord,
    KillSwitch,
    LiveAccessAttempt,
    LiveExecutionRefused,
    LiveVenuePort,
    LiveVenueRegistry,
    OrderRecord,
    Side,
    UnconfiguredLiveVenue,
)
from core.domain.base import Kind, Ref
from core.domain.execution import ExecutionMode
from core.domain.specs import Instrument, InstrumentType
from core.lifecycle.strategy import AuthorizationRecord, RiskGateRecord

REPO = Path(__file__).resolve().parents[2]
LIVE_VENUE_FILE = REPO / "apps" / "execution" / "live_venue.py"

T0 = datetime(2026, 9, 28, tzinfo=UTC)
SUBJECT = Ref(kind=Kind.STRATEGY, name="btc-momentum", version="1.0.0")
OTHER_SUBJECT = Ref(kind=Kind.STRATEGY, name="someone-else", version="1.0.0")
BTC = Instrument(
    venue="sim", symbol="BTCUSDT", instrument_type=InstrumentType.SPOT, base="BTC", quote="USDT"
)


def _order() -> OrderRecord:
    return OrderRecord(
        sequence=0,
        deployment_id="dep-1",
        artifact_id="a" * 64,
        stage=ExecutionStage.SIMULATED,
        mode=ExecutionMode.SIMULATED,
        instrument=BTC,
        side=Side.BUY,
        quantity=Decimal("1"),
        reference_price=Decimal("100"),
        submitted_at=T0,
    )


def _authorization(subject: Ref = SUBJECT) -> AuthorizationRecord:
    return AuthorizationRecord(
        subject=subject,
        authorized_by="raphael",
        risk_budget="operator-supplied",
        authorized_at=T0,
        valid_until=T0 + timedelta(days=1),
    )


def _risk_gate(subject: Ref = SUBJECT, *, passed: bool = True) -> RiskGateRecord:
    return RiskGateRecord(subject=subject, gate_id="pre-live-check", passed=passed, checked_at=T0)


# -- UnconfiguredLiveVenue: every method refuses and leaves an audit trail ------------------------


def test_place_order_refuses_and_records_attempt() -> None:
    venue = UnconfiguredLiveVenue()
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        venue.place_order(_order(), T0)
    assert len(venue.attempts) == 1
    attempt = venue.attempts[0]
    assert isinstance(attempt, LiveAccessAttempt)
    assert attempt.method == "place_order"
    assert attempt.deployment_id == "dep-1"
    assert attempt.sequence == 0


def test_cancel_order_refuses_and_records_attempt() -> None:
    venue = UnconfiguredLiveVenue()
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        venue.cancel_order("some-order-id", T0)
    assert venue.attempts[0].method == "cancel_order"


def test_positions_refuses_and_records_attempt() -> None:
    venue = UnconfiguredLiveVenue()
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        venue.positions("dep-1", T0)
    assert venue.attempts[0].method == "positions"
    assert venue.attempts[0].deployment_id == "dep-1"


def test_fills_refuses_and_records_attempt() -> None:
    venue = UnconfiguredLiveVenue()
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        venue.fills("dep-1", T0)
    assert venue.attempts[0].method == "fills"


def test_heartbeat_refuses_and_records_attempt() -> None:
    venue = UnconfiguredLiveVenue()
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        venue.heartbeat(T0)
    assert venue.attempts[0].method == "heartbeat"
    assert venue.attempts[0].deployment_id is None


def test_attempts_accumulate_in_order_across_every_method() -> None:
    venue = UnconfiguredLiveVenue()
    for call in (
        lambda: venue.place_order(_order(), T0),
        lambda: venue.cancel_order("oid", T0),
        lambda: venue.positions("dep-1", T0),
        lambda: venue.fills("dep-1", T0),
        lambda: venue.heartbeat(T0),
    ):
        with pytest.raises(LiveExecutionRefused):
            call()
    assert [a.sequence for a in venue.attempts] == [0, 1, 2, 3, 4]
    assert [a.method for a in venue.attempts] == [
        "place_order",
        "cancel_order",
        "positions",
        "fills",
        "heartbeat",
    ]
    # content-addressed and distinct despite very similar payloads
    assert len({a.record_id for a in venue.attempts}) == 5


def test_on_record_hook_is_invoked_before_the_refusal_propagates() -> None:
    sink: list[LiveAccessAttempt] = []
    venue = UnconfiguredLiveVenue(on_record=sink.append)
    with pytest.raises(LiveExecutionRefused):
        venue.heartbeat(T0)
    assert len(sink) == 1
    assert sink[0] is venue.attempts[0]


def test_unconfigured_live_venue_implements_the_port() -> None:
    assert isinstance(UnconfiguredLiveVenue(), LiveVenuePort)


# -- Registry: only LiveVenuePort implementations may register ------------------------------------


class _CompliantFakeVenue:
    """Structurally satisfies LiveVenuePort. Test-only; never instantiated (build() always
    refuses before construction, since LIVE_TRADING_ENABLED is False)."""

    def place_order(self, order: OrderRecord, at: datetime) -> FillRecord:  # pragma: no cover
        raise NotImplementedError

    def cancel_order(self, order_id: str, at: datetime) -> None:  # pragma: no cover
        raise NotImplementedError

    def positions(self, deployment_id: str, at: datetime) -> dict[str, Decimal]:  # pragma: no cover
        raise NotImplementedError

    def fills(self, deployment_id: str, at: datetime) -> tuple[FillRecord, ...]:  # pragma: no cover
        raise NotImplementedError

    def heartbeat(self, at: datetime) -> bool:  # pragma: no cover
        raise NotImplementedError


class _IncompleteFakeVenue:
    """Missing every method but place_order — must not be registrable."""

    def place_order(self, order: OrderRecord, at: datetime) -> FillRecord:  # pragma: no cover
        raise NotImplementedError


def test_registry_default_registers_only_unconfigured() -> None:
    registry = LiveVenueRegistry()
    assert registry.registered == {"unconfigured": UnconfiguredLiveVenue}


def test_registry_rejects_a_class_that_does_not_implement_the_port() -> None:
    registry = LiveVenueRegistry()
    with pytest.raises(TypeError, match="LiveVenuePort"):
        registry.register("incomplete", _IncompleteFakeVenue)
    with pytest.raises(TypeError, match="LiveVenuePort"):
        registry.register("not-even-a-venue", object)
    assert "incomplete" not in registry.registered
    assert "not-even-a-venue" not in registry.registered


def test_registry_accepts_a_class_that_structurally_implements_the_port() -> None:
    registry = LiveVenueRegistry()
    registry.register("fake", _CompliantFakeVenue)
    assert registry.registered["fake"] is _CompliantFakeVenue


# -- LIVE_TRADING_ENABLED: fixed False, not overridable by param / config / env -------------------


def test_live_trading_enabled_is_a_false_module_constant() -> None:
    assert LIVE_TRADING_ENABLED is False


def test_build_has_no_parameter_that_could_override_the_switch() -> None:
    signature = inspect.signature(LiveVenueRegistry.build)
    names = set(signature.parameters)
    assert "live_trading_enabled" not in names
    assert "enabled" not in names
    assert "force" not in names
    assert "override" not in names
    # every parameter besides self/name is keyword-only, so nothing can be smuggled in positionally
    assert {"self", "name", "subject", "authorization", "risk_gate", "kill_switch"} == names


def test_environment_variables_do_not_flip_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LIVE_TRADING_ENABLED",
        "HLENS_LIVE_TRADING_ENABLED",
        "EXECUTION_LIVE_ENABLED",
        "ENABLE_LIVE_TRADING",
    ):
        monkeypatch.setenv(var, "true")
    import importlib

    from apps.execution import live_venue as live_venue_module

    importlib.reload(live_venue_module)
    try:
        assert live_venue_module.LIVE_TRADING_ENABLED is False
    finally:
        importlib.reload(live_venue_module)  # restore a clean module for any later test


def test_module_source_never_reads_environment_or_files_for_the_switch() -> None:
    source = LIVE_VENUE_FILE.read_text(encoding="utf-8")
    for needle in ("os.environ", "os.getenv", "getenv(", "open(", "dotenv"):
        assert needle not in source, f"live_venue.py must not read {needle!r} (H9)"


def test_registry_build_refuses_even_when_authorization_risk_gate_and_kill_switch_all_pass() -> (
    None
):
    registry = LiveVenueRegistry()
    registry.register("fake", _CompliantFakeVenue)
    kill_switch = KillSwitch()
    with pytest.raises(LiveExecutionRefused, match="LIVE_TRADING_ENABLED"):
        registry.build(
            "fake",
            subject=SUBJECT,
            authorization=_authorization(),
            risk_gate=_risk_gate(),
            kill_switch=kill_switch,
        )


def test_registry_build_default_unconfigured_refuses_with_every_reason_listed() -> None:
    registry = LiveVenueRegistry()
    kill_switch = KillSwitch()
    kill_switch.trip(reason="drill", tripped_by="test", at=T0)
    with pytest.raises(LiveExecutionRefused) as excinfo:
        registry.build(
            "unconfigured",
            subject=SUBJECT,
            authorization=None,
            risk_gate=None,
            kill_switch=kill_switch,
        )
    message = str(excinfo.value)
    assert "LIVE_TRADING_ENABLED" in message
    assert "AuthorizationRecord" in message
    assert "RiskGateRecord" in message
    assert "kill switch is tripped" in message
    assert "UnconfiguredLiveVenue" in message


def test_registry_build_rejects_authorization_and_risk_gate_for_another_subject() -> None:
    registry = LiveVenueRegistry()
    with pytest.raises(LiveExecutionRefused) as excinfo:
        registry.build(
            "unconfigured",
            subject=SUBJECT,
            authorization=_authorization(OTHER_SUBJECT),
            risk_gate=_risk_gate(OTHER_SUBJECT),
            kill_switch=KillSwitch(),
        )
    assert "another subject" in str(excinfo.value)


def test_registry_build_unknown_adapter_name_refuses() -> None:
    registry = LiveVenueRegistry()
    with pytest.raises(LiveExecutionRefused, match="no live adapter is registered"):
        registry.build(
            "does-not-exist",
            subject=SUBJECT,
            authorization=_authorization(),
            risk_gate=_risk_gate(),
            kill_switch=KillSwitch(),
        )


# -- ExecutionLadder: live rungs are still always refused, now citing ADR-0084 --------------------


def test_ladder_request_live_still_refuses_and_now_cites_adr_0084() -> None:
    ticks = iter(range(10))
    ladder = ExecutionLadder(
        deployment_id="dep-1", subject=SUBJECT, clock=lambda: T0 + timedelta(seconds=next(ticks))
    )
    ladder.promote_to_paper(evidence=("sim run reviewed",), decided_by="raphael")
    with pytest.raises(LiveExecutionRefused, match="ADR-0084"):
        ladder.request_live(
            ExecutionStage.SMALL_LIVE,
            requested_by="raphael",
            authorization=_authorization(),
        )
    record = ladder.history[-1]
    assert record.granted is False
    assert record.to_stage is ExecutionStage.SMALL_LIVE
    assert "ADR-0084" in record.reason


# -- static check: no network / exchange SDK import anywhere in apps/execution --------------------

_FORBIDDEN_ROOTS = {
    "research",
    "socket",
    "ssl",
    "http",
    "urllib",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "websockets",
    "ccxt",
    "binance",
    "grpc",
    "infrastructure",
    "os",  # this build never reads env vars for the live switch or any credential
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_apps_execution_still_has_no_network_or_exchange_sdk_imports() -> None:
    files = list((REPO / "apps" / "execution").rglob("*.py"))
    assert files
    for path in files:
        leaked = _imported_roots(path) & _FORBIDDEN_ROOTS
        assert not leaked, f"{path.relative_to(REPO)} violates the execution red line: {leaked}"
