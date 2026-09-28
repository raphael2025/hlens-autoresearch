# ADR-0086: 验证与生命周期收口：门集完整性、退役记录存储、Outcome 输入错误映射

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权（CLAUDE.md §0） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 4 / 5 / 8 / 11 |
| 影响范围 | Research（validation / promotion / strategies / loop）、Infrastructure（registry）；**不改** `core/` 契约、Constitution、Profile 或阈值 |
| 是否破坏兼容 | 否：只收紧拒绝条件，并新增只追加存储 |

## 背景

MOD-VALID（2026-09-28）实现了 ADR-0013 的报告 ↔ Profile 阈值核验，同时提出三个未定义点：

1. 没有任何已接受规则说明一个报告必须包含哪些门。`docs/architecture/07-validation.md` §2.1 把「门集完整性」列为待实现。
2. `RetirementRecord` 只有契约类型，没有任何写入者或存储。
3. C-L2 的 Outcome 输入守卫（O-1）抛出 `OutcomeUsedAsInput(ValueError)`；`evaluate_strategy` 会把它记为 `CONTRACT_VIOLATION`，而 `core/errors` 里已经有专门的 `ReasonCode.OUTCOME_USED_AS_INPUT`（类别 LEAKAGE）。

## 决策

1. **门集完整性按流水线版本定义。** 在 `research/validation` 中为每个验证流水线版本定义必需门集常量：
   - 样本内模式：G0–G4 的全部门 ID。
   - 封存 OOS 模式：在样本内门集基础上加上 G5。
   - `verify_report` 核对报告的门 ID 集合，缺门或多出未知门时都拒绝。
   - Promotion 在阈值核验之后，以原因 `report_gate_set_incomplete` 拒绝不完整的报告。
   - 门集随流水线版本一起变化；报告按它自己记录的流水线版本核验，旧报告不追溯。
2. **退役记录由 Control Plane 只追加存储。** 新增 `infrastructure/registry/retirement.py`：
   - 基于 `research/persistence/journal.py` 的哈希链只追加日志；
   - 记录格式为 `core` 中已有的 `RetirementRecord`；
   - 同一对象重复退役时拒绝；
   - 提供读取与校验接口；
   - 写入由 `research/evolution/operators.py::retire` 的调用方显式传入存储，不引入全局单例。
3. **Outcome 输入错误的映射。**
   - 验证流水线遇到 `OutcomeUsedAsInput` 时，`evaluate_strategy` 记为 REJECTED，原因 `ReasonCode.OUTCOME_USED_AS_INPUT`（LEAKAGE），并写入 Failure Registry（H6）。
   - 研究循环（`research/loop`）按同一原因记为该 trial 被拒绝，**不当作技术故障重试**。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 由 Profile 声明必需门集 | 需要改 Profile 契约结构；按流水线版本定义已经足够表达，也不会触及冻结契约 |
| 退役记录放在 `research/strategies` | 退役对象是已晋升（ACTIVE）的策略，属于 Control Plane 生命周期事实 |
| 保持 `CONTRACT_VIOLATION` | 会丢失 C-L2 泄漏这一语义，Failure Registry 的原因统计也会失真 |

## 后果

- 正面：验证报告不能再漏门蒙混晋升；退役有了可审计的持久记录；泄漏类失败的原因如实归类。
- 代价：以前能通过 Promotion 的缺门报告现在会被拒绝。目前没有任何策略晋升过，所以没有实际影响。

## 实施记录（2026-09-28，PM 审阅）

- 决策 1 已实现，入口为 `research/validation/gate_set.py`、`verification.py` 与 Promotion 的 `report_gate_set_incomplete`。PM 接受以下三处实现选择：
  - **按阶段检查**：检查粒度是阶段前缀（G0–G5），不是具体的门 ID。原因是具体门 ID 会随所绑定的 Profile 变化，例如成本压力档位、各 seed 的对照、基准条目。
  - **独立 G5 报告**：除样本内报告、样本内加 G5 的报告外，承认第三种形态，即只含 G5 的独立报告，其必需门集为 `{G5}`，这也是现有系统的实际产出。
  - **流水线版本**：目前只登记代码实际记录的版本 `0.2.0-draft`，新版本在开始被记录时再登记。
- 决策 2 已实现：`infrastructure/registry/retirement.py`。按依赖方向，底层复用 `infrastructure/event_bus/journal.py`，而不是 `research/persistence/journal.py`。
- 决策 3：循环一侧由 IMPL-LOOP 实现；`research/strategies/pipeline.py::evaluate_strategy` 一侧由后续提交完成。
