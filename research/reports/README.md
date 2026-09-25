# research/reports

研究侧的报告**写入方**（[ADR-0048](../../docs/adr/0048-api-and-web-console.md) Implementation note, report writer）。
状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

`apps/api` 的只读研究控制台（`apps/api/store.py` 的 `ReportStore`）只**读取**
`<report_root>/<kind>/<id>.json`，从不 import `research/`（01-system.md §3）。本目录是配套的写入方：
每种报告一个 writer 函数，产出 `ReportStore` 能直接读回的 JSON 文件。`apps/` 与 `research/` 仍然互不
import——两边只通过这份文件格式约定耦合。

| 模块 | 内容 |
|---|---|
| `envelope.py` | 通用底层写入 `write_report_file(root, kind, id, payload)`：确定性 JSON、append-only、拒绝覆盖 |
| `validation.py` | `write_validation_report`：Phase 4 `ValidationReport`（`research/validation/pipeline.py` 的 `build_report`） |
| `loop.py` | `write_research_loop_round[s]`：Phase 11 `LoopRecord`（`apps/worker/loop.py`，经 `research/loop/compose.py` 组合） |
| `matrix.py` | `write_state_strategy_matrix`：Phase 6 `StateStrategyMatrix`（`research/experiments/state_strategy.py` 的 `matrix_from_backtest`） |
| `router.py` | `write_router_paper_run`：Phase 10 `RouterPaperRun`（`research/router/paper.py` 的 `paper_run`） |

## 信封 / 哈希规则

`ReportStore` 读取时自己从解析后的 payload 重新计算 `content_hash`（sorted-key 规范 JSON 的
SHA-256），因此写入方**不需要**复现它的信封或逐字节匹配它的哈希算法——那是读取方的职责，且与磁盘上的
具体格式无关。写入方只负责另外两件事：

1. **确定性 JSON**：payload 落盘前已经是纯 JSON 安全值（`Decimal` → `str`、时间 → UTC ISO-8601、
   枚举 → 其 value），序列化用 `core.domain.base.canonical_json`（sorted keys、紧凑分隔符）——
   这是项目里唯一一份"规范 JSON"定义（`docs/architecture/02-domain.md` §3，`Contract.content_hash()`
   同样用它），而不是复用 `apps/api/store.py` 私有的、下划线开头的 `_canonical_json`
   （两者仅 `ensure_ascii` 不同，是纯格式差异；复用私有实现只会让"规范 JSON"多出第二个定义，
   且没有任何好处——`ReportStore` 本来就不看磁盘上的原始格式）。
2. **Append-only**：`<kind>/<id>.json` 已存在时，内容相同 = 原地不动（no-op）；内容不同 = 拒绝写入
   （`ReportConflict`），绝不静默覆盖。

`id` 一律取**对象自身已有的内容 / 结果哈希**，四种报告分别是：`ValidationReport.content_hash()`
（不是外部赋予、非内容身份的 `report_id`，见该模型自身文档字符串）、`LoopRecord.record_hash`、
`StateStrategyMatrix.matrix_hash`、`RouterPaperRun.run_hash`。同一对象重复写入因此天然幂等；
两个不同内容的对象天然拿到不同 id，不会互相覆盖——`ReportConflict` 主要是防御性的（例如手工损坏的
文件），并由 `envelope.py` 的单元测试直接触发验证。

## 用法

```python
from pathlib import Path

from research.reports import write_validation_report

written = write_validation_report(Path("var/reports"), report)
written.id  # 用于 GET /reports/validation_report/{id}
written.written  # False = 内容相同的 no-op
```

Phase 11 循环的接线在 `research/loop/compose.py` 的 `run_unattended_and_report`：
`reports_root=None`（默认）等价于直接调用 `ResearchLoop.run_unattended`，不落盘；给定路径时，
每一轮的 `LoopRecord` 都会额外写入该目录。

## 未完成（调试批次）

四种 writer 目前各自独立构造 payload；`state_strategy_matrix` / `router_paper_run` 的 payload 字段是
按显示需要挑选的摘要（例如路由纸面运行不落盘完整合并目标仓位序列），不是对应对象的逐字段完整转储，
必要时可以扩展。没有写端点/鉴权/清理策略——这些由控制台的写路径（ADR-0048 §3 提到的 P7/P8/P11
框架落地后再暴露）与运维决定。
