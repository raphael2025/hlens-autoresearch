"""Reserved live-trading interface — declared, wired to nothing, refused (ADR-0084).

Raphael has not authorized live trading (CLAUDE.md H10; ADR-0046's red line is unchanged). This
module only reserves the *shape* a future live venue and credential source would have to satisfy,
so that Phase 5 / execution-plane code can be written against a stable port today. It does not
implement a real venue, does not perform network I/O, and does not read any credential, environment
variable or file (H9).

``LiveVenuePort`` and ``CredentialProvider`` are ``Protocol`` declarations only — interfaces, not
behavior. The single concrete class in this module, ``UnconfiguredLiveVenue``, implements
``LiveVenuePort`` by recording every call and then refusing it with ``LiveExecutionRefused``.

``LIVE_TRADING_ENABLED`` is a module-level constant fixed to ``False`` in this build. It is not a
setting: nothing in this module reads it from configuration, an environment variable or a caller-
supplied argument, and ``LiveVenueRegistry.build`` closes over the module constant directly rather
than accepting it as a parameter, so there is no call-site path that can flip it. Turning it on
needs a new build with Raphael's explicit authorization and a new ADR (ADR-0084 §"后果").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from apps.execution.errors import LiveExecutionRefused
from apps.execution.kill_switch import KillSwitch
from apps.execution.records import FillRecord, LiveAccessAttempt, OrderRecord
from core.domain.base import Ref
from core.lifecycle.strategy import AuthorizationRecord, RiskGateRecord

__all__ = [
    "LIVE_TRADING_ENABLED",
    "CredentialProvider",
    "LiveVenuePort",
    "LiveVenueRegistry",
    "UnconfiguredLiveVenue",
]

#: Fixed off in this build (ADR-0084). Not read from env/config; not a constructor or call
#: parameter anywhere in this module — see the module docstring.
LIVE_TRADING_ENABLED: Final[bool] = False


@runtime_checkable
class LiveVenuePort(Protocol):
    """The shape a real live-trading venue adapter would have to implement.

    Method names and signatures are aligned with ``apps.execution.venue.SimulatedVenue``: order
    placement takes a recorded ``OrderRecord`` and a clock timestamp and returns a ``FillRecord``;
    positions and fills are queried per deployment as of a clock timestamp; ``heartbeat`` reports
    connectivity at a timestamp. No implementation of this port ships in this build other than
    ``UnconfiguredLiveVenue``, which refuses every method.
    """

    def place_order(self, order: OrderRecord, at: datetime) -> FillRecord: ...

    def cancel_order(self, order_id: str, at: datetime) -> None: ...

    def positions(self, deployment_id: str, at: datetime) -> Mapping[str, Decimal]: ...

    def fills(self, deployment_id: str, at: datetime) -> tuple[FillRecord, ...]: ...

    def heartbeat(self, at: datetime) -> bool: ...


@runtime_checkable
class CredentialProvider(Protocol):
    """The shape a future credential source would have to implement — interface only.

    This build never implements this Protocol, never calls it, and never reads an environment
    variable, file or secret store (H9). It exists purely so a real adapter's constructor can be
    typed against it once one is authorized and written under a future ADR.
    """

    def identity(self) -> str: ...

    def is_available(self) -> bool: ...


class UnconfiguredLiveVenue:
    """The only ``LiveVenuePort`` implementation in this build: records, then refuses, always.

    No network, no account, no credential — there is nothing behind this class to connect to.
    Every method appends a ``LiveAccessAttempt`` to this instance's own audit history *before*
    raising ``LiveExecutionRefused``, so a caller can prove what was attempted even though nothing
    was ever sent anywhere. An optional ``on_record`` hook (same convention as
    ``ExecutionLadder``) lets an owner fan the attempt into its own audit sink.
    """

    def __init__(self, *, on_record: Callable[[LiveAccessAttempt], None] | None = None) -> None:
        self._attempts: list[LiveAccessAttempt] = []
        self._on_record = on_record

    @property
    def attempts(self) -> tuple[LiveAccessAttempt, ...]:
        return tuple(self._attempts)

    def _refuse(self, method: str, deployment_id: str | None, at: datetime) -> None:
        reason = (
            f"{method} is refused: this build has no live venue (ADR-0084); "
            "live trading needs Raphael's explicit authorization and a new ADR"
        )
        record = LiveAccessAttempt(
            sequence=len(self._attempts),
            method=method,
            deployment_id=deployment_id,
            reason=reason,
            attempted_at=at,
        )
        self._attempts.append(record)
        if self._on_record is not None:
            self._on_record(record)
        raise LiveExecutionRefused(f"{reason} (attempt {record.record_id})")

    def place_order(self, order: OrderRecord, at: datetime) -> FillRecord:
        self._refuse("place_order", order.deployment_id, at)
        raise AssertionError("unreachable: _refuse always raises")

    def cancel_order(self, order_id: str, at: datetime) -> None:
        self._refuse("cancel_order", None, at)

    def positions(self, deployment_id: str, at: datetime) -> Mapping[str, Decimal]:
        self._refuse("positions", deployment_id, at)
        raise AssertionError("unreachable: _refuse always raises")

    def fills(self, deployment_id: str, at: datetime) -> tuple[FillRecord, ...]:
        self._refuse("fills", deployment_id, at)
        raise AssertionError("unreachable: _refuse always raises")

    def heartbeat(self, at: datetime) -> bool:
        self._refuse("heartbeat", None, at)
        raise AssertionError("unreachable: _refuse always raises")


class LiveVenueRegistry:
    """Adapter registry for live venues — only ``LiveVenuePort`` implementations may register.

    ``"unconfigured"`` always resolves to ``UnconfiguredLiveVenue`` by default. ``build`` is the
    only way to obtain an adapter, and it always refuses in this build: ``LIVE_TRADING_ENABLED``
    is closed over as the module constant (not a parameter), so no caller can supply ``True`` to
    make it pass, regardless of how valid the authorization, risk gate and kill switch state are.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, type[LiveVenuePort]] = {"unconfigured": UnconfiguredLiveVenue}

    @property
    def registered(self) -> Mapping[str, type[LiveVenuePort]]:
        return dict(self._adapters)

    def register(self, name: str, venue_cls: type) -> None:
        """Register ``venue_cls`` under ``name``; refuses anything that is not a
        ``LiveVenuePort``.
        """
        if not name:
            raise ValueError("name must be non-empty")
        if not (isinstance(venue_cls, type) and issubclass(venue_cls, LiveVenuePort)):
            raise TypeError(
                f"{venue_cls!r} does not implement LiveVenuePort and cannot be registered "
                "(ADR-0084)"
            )
        self._adapters[name] = venue_cls

    def build(
        self,
        name: str,
        *,
        subject: Ref,
        authorization: AuthorizationRecord | None,
        risk_gate: RiskGateRecord | None,
        kill_switch: KillSwitch,
    ) -> LiveVenuePort:
        """Construct the adapter registered as ``name`` — always refused in this build.

        Checks, all of which must hold before anything but ``UnconfiguredLiveVenue`` could ever be
        returned: a matching, valid ``AuthorizationRecord``; a passing ``RiskGateRecord`` for the
        same subject; an untripped ``KillSwitch``; and the module constant
        ``LIVE_TRADING_ENABLED``, which is always ``False`` here. There is no parameter on this
        method (or anywhere else in this module) that can override that constant.
        """
        venue_cls = self._adapters.get(name)
        if venue_cls is None:
            raise LiveExecutionRefused(f"no live adapter is registered as {name!r} (ADR-0084)")
        reasons: list[str] = []
        if not LIVE_TRADING_ENABLED:
            reasons.append("LIVE_TRADING_ENABLED is False in this build (ADR-0084)")
        if authorization is None:
            reasons.append("no AuthorizationRecord was supplied")
        elif authorization.subject.target_identity() != subject.target_identity():
            reasons.append("the AuthorizationRecord belongs to another subject")
        if risk_gate is None:
            reasons.append("no RiskGateRecord was supplied")
        elif not risk_gate.passed:
            reasons.append("the RiskGate did not pass")
        elif risk_gate.subject.target_identity() != subject.target_identity():
            reasons.append("the RiskGateRecord belongs to another subject")
        if kill_switch.tripped:
            reasons.append("the kill switch is tripped")
        if venue_cls is UnconfiguredLiveVenue:
            reasons.append("no real adapter is registered; the default is UnconfiguredLiveVenue")
        if reasons:
            raise LiveExecutionRefused("; ".join(reasons) + " (ADR-0084)")
        return venue_cls()  # pragma: no cover - unreachable while LIVE_TRADING_ENABLED is False
