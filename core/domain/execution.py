"""执行模式的共享领域词汇（ADR-0006 §2、ADR-0011 D-17.5）。

`ExecutionMode` 既被生命周期状态机（`core/lifecycle/strategy.py`）使用，
也被退役记录（`core/domain/research.py`）使用。它定义在 domain 层，
是为了满足 01-system.md §4 的依赖规则：**Domain 只依赖标准库与 Pydantic**，
不得反向依赖 `core/lifecycle`。`core/lifecycle/strategy.py` 重导出同名类型，
因此 `from core.lifecycle.strategy import ExecutionMode` 仍然有效，
Schema 中的枚举名称也保持稳定。

权威定义只有这一处，不得在别处复制第二个同义枚举。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ExecutionMode"]


class ExecutionMode(StrEnum):
    """ACTIVE 的属性，不是生命周期状态（ADR-0006 §2）。"""

    SIMULATED = "SIMULATED"
    LIVE = "LIVE"
