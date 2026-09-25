"""Independent execution service, SIMULATION ONLY (roadmap Phase 13; ADR-0046).

No live venue, no credentials, no network I/O. ``ExecutionMode.LIVE`` is refused. Never imports
research/ (tests/test_architecture_boundaries.py).
"""

from apps.execution.audit import AuditRecord, AuditTrail
from apps.execution.book import PositionBook
from apps.execution.drill import DrillReport, run_kill_switch_drill
from apps.execution.errors import (
    DeploymentNotAdmitted,
    ExecutionRefused,
    LadderGateRefused,
    LiveExecutionRefused,
)
from apps.execution.kill_switch import KillSwitch
from apps.execution.ladder import LIVE_REFUSAL_MESSAGE, ExecutionLadder
from apps.execution.monitor import AlertHook, Monitor, MonitorSnapshot
from apps.execution.records import (
    LIVE_STAGES,
    STAGE_ORDER,
    Alert,
    AlertKind,
    ExecutionStage,
    FillRecord,
    KillSwitchTrip,
    LadderGateRecord,
    OrderRecord,
    RejectionRecord,
    RejectionSource,
    Side,
    TargetPosition,
    TargetPositions,
    instrument_key,
)
from apps.execution.risk import RISK_POLICY_LIMIT_KEYS, RiskLimits, SecondLineRisk
from apps.execution.service import (
    RUNNABLE_LIFECYCLE_STATES,
    TOPICS,
    ExecutionReport,
    ExecutionService,
    TargetPositionSource,
)
from apps.execution.strategy_source import StrategyProviderTargetSource, StrategySourceRefused
from apps.execution.venue import CostModel, LinearCostModel, SimulatedVenue

__all__ = [
    "LIVE_REFUSAL_MESSAGE",
    "LIVE_STAGES",
    "RISK_POLICY_LIMIT_KEYS",
    "RUNNABLE_LIFECYCLE_STATES",
    "STAGE_ORDER",
    "TOPICS",
    "Alert",
    "AlertHook",
    "AlertKind",
    "AuditRecord",
    "AuditTrail",
    "CostModel",
    "DeploymentNotAdmitted",
    "DrillReport",
    "ExecutionLadder",
    "ExecutionRefused",
    "ExecutionReport",
    "ExecutionService",
    "ExecutionStage",
    "FillRecord",
    "KillSwitch",
    "KillSwitchTrip",
    "LadderGateRecord",
    "LadderGateRefused",
    "LinearCostModel",
    "LiveExecutionRefused",
    "Monitor",
    "MonitorSnapshot",
    "OrderRecord",
    "PositionBook",
    "RejectionRecord",
    "RejectionSource",
    "RiskLimits",
    "SecondLineRisk",
    "Side",
    "SimulatedVenue",
    "StrategyProviderTargetSource",
    "StrategySourceRefused",
    "TargetPosition",
    "TargetPositionSource",
    "TargetPositions",
    "instrument_key",
    "run_kill_switch_drill",
]
