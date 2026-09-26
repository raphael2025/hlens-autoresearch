"""Independent execution service, SIMULATION ONLY (roadmap Phase 13; ADR-0046).

No live venue, no credentials, no network I/O. ``ExecutionMode.LIVE`` is refused. Never imports
research/ (tests/test_architecture_boundaries.py).
"""

from apps.execution.audit import (
    AuditCorrupted,
    AuditRecord,
    AuditReplay,
    AuditTrail,
    replay_audit,
)
from apps.execution.book import PositionBook
from apps.execution.drill import DrillReport, run_kill_switch_drill
from apps.execution.errors import (
    DeploymentNotAdmitted,
    ExecutionRefused,
    KillSwitchEngaged,
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
    MarkPrice,
    MarkRecord,
    OrderRecord,
    RejectionRecord,
    RejectionSource,
    Side,
    TargetPosition,
    TargetPositions,
    instrument_key,
)
from apps.execution.risk import RISK_POLICY_LIMIT_KEYS, RiskLimits, SecondLineRisk
from apps.execution.risk_replay import RiskReplay, RiskReplayDiverged, replay_risk
from apps.execution.service import (
    RESTORE_TRIPPED_BY,
    RUNNABLE_LIFECYCLE_STATES,
    TOPICS,
    ExecutionReport,
    ExecutionService,
    MarksChoiceRequired,
    TargetPositionSource,
)
from apps.execution.strategy_source import (
    EquityPriceSizer,
    PositionSizer,
    StrategyProviderTargetSource,
    StrategySourceRefused,
)
from apps.execution.venue import CostModel, LinearCostModel, SimulatedVenue

__all__ = [
    "LIVE_REFUSAL_MESSAGE",
    "LIVE_STAGES",
    "RESTORE_TRIPPED_BY",
    "RISK_POLICY_LIMIT_KEYS",
    "RUNNABLE_LIFECYCLE_STATES",
    "STAGE_ORDER",
    "TOPICS",
    "Alert",
    "AlertHook",
    "AlertKind",
    "AuditRecord",
    "AuditCorrupted",
    "AuditReplay",
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
    "MarkPrice",
    "MarkRecord",
    "MarksChoiceRequired",
    "LiveExecutionRefused",
    "KillSwitchEngaged",
    "Monitor",
    "MonitorSnapshot",
    "OrderRecord",
    "PositionBook",
    "RejectionRecord",
    "RejectionSource",
    "RiskLimits",
    "RiskReplay",
    "RiskReplayDiverged",
    "SecondLineRisk",
    "Side",
    "SimulatedVenue",
    "StrategyProviderTargetSource",
    "StrategySourceRefused",
    "EquityPriceSizer",
    "PositionSizer",
    "TargetPosition",
    "TargetPositionSource",
    "TargetPositions",
    "instrument_key",
    "replay_audit",
    "replay_risk",
    "run_kill_switch_drill",
]
