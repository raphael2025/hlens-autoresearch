"""ExperimentRun 执行状态机（06-experiment.md §5）。

与研究对象的晋升状态机（core/lifecycle/strategy.py）是**两个不同的状态机**。
"""

from __future__ import annotations

from core.domain.research import RunState
from core.errors import LifecycleViolation

__all__ = ["ALLOWED_RUN_TRANSITIONS", "TERMINAL_RUN_STATES", "validate_run_transition"]

R = RunState

ALLOWED_RUN_TRANSITIONS: frozenset[tuple[RunState, RunState]] = frozenset(
    {
        (R.REGISTERED, R.QUEUED),
        (R.QUEUED, R.RUNNING),
        (R.RUNNING, R.COMPLETED),
        (R.RUNNING, R.ERRORED),
        (R.COMPLETED, R.VALIDATING),
        (R.VALIDATING, R.VALIDATED),
    }
)

TERMINAL_RUN_STATES: frozenset[RunState] = frozenset({R.VALIDATED, R.ERRORED})


def validate_run_transition(from_state: RunState, to_state: RunState) -> None:
    """校验一次 Run 状态转移；非法时抛 `LifecycleViolation`。"""
    if from_state in TERMINAL_RUN_STATES:
        raise LifecycleViolation(f"{from_state} 是 Run 的终态，不得再转移")
    if (from_state, to_state) not in ALLOWED_RUN_TRANSITIONS:
        raise LifecycleViolation(f"不允许的 Run 转移：{from_state} → {to_state}")
