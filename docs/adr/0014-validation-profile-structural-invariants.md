# ADR-0014: Validation Profile 的普适结构不变量

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24 起草，等待 Codex 文档复核；未获批准，不得实施） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3）；数值仍属 Phase 4 |
| 影响范围 | Contract / Validation |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0013](0013-deterministic-verdict-and-finite-numbers.md) |

## 背景

`core/contracts/validation_profile.py` 的字段已按 `07-validation.md` §5 落成，但其中若干
时间跨度与倍数字段没有任何符号约束：`WalkForwardParams.train_window / test_window / step`
（`:54-56`）、`DataSplitParams.sealed_oos_length / sealed_oos_max_extension / embargo`
（`:65-68`）、`CostStressParams.stress_multipliers`（`:123`）、
`LifecycleParams.paper_period`（`:132`）都可以是零或负数。`CostStressParams.cost_model`
（`:121`）是 `Ref`，但未校验其 `kind`。

零或负的 walk-forward 窗口、零长度的封存 OOS、零的成本压力倍数在任何标的、任何周期、
任何研究类别下都不是"另一种校准选择"，而是**结构上无意义的配置**。这类约束与 D-09 的
数值选择无关，属于两步冻结的 Step 1（结构），可以在 Phase 0 冻结。

## 精确决定

本 ADR 只冻结**普适的结构不变量**：在所有可能的 Profile 中都必须成立、且与具体数值选择无关的约束。

| 字段 | 规则 |
|---|---|
| `WalkForwardParams.train_window` | `> 0` |
| `WalkForwardParams.test_window` | `> 0` |
| `WalkForwardParams.step` | `> 0` |
| `DataSplitParams.sealed_oos_length` | `> 0`（封存区必须有长度，否则不构成样本外检验） |
| `DataSplitParams.embargo` | `>= 0`（零表示不设隔离期，是合法配置） |
| `DataSplitParams.sealed_oos_max_extension` | `>= 0`（零表示不允许延长） |
| `CostStressParams.stress_multipliers` 的每一项 | `> 0` 且**有限** |
| `CostStressParams.reported_only_multipliers` 的每一项 | 同上 |
| `CostStressParams.cost_model` | `kind` 必须是 `cost_model` |
| `LifecycleParams.paper_period` | `> 0`（Paper Trading 观察期必须有长度） |

**有限数约束复用 [ADR-0013](0013-deterministic-verdict-and-finite-numbers.md) §D-20.1**：
Profile 中所有浮点字段（含映射与序列内的数值）在校验阶段拒绝 NaN 与 ±Infinity。
本 ADR 不重复定义该规则，只声明它同样适用于 Profile。

`DataSplitParams._boundary_after_start`（`:71-75`）等既有校验保持不变。

## 明确不做

- **不选择 Phase 4 的任何数值。** 不给出窗口长度、封存 OOS 长度、embargo 长度、
  压力倍数、观察期长度或任何阈值的建议值、默认值或范围（`[0,1]` 这类**结构**范围除外，
  见 ADR-0013 §D-20.2）。数值在 Phase 4 校准后按两步冻结 Step 2 写入具体 Profile 版本。
- **不解决 H-3 ~ H-7**（封存边界与轮换、目标假阳性率、现货 / 永续差异、研究类别划分等，
  见 ADR-0007 §5）。
- **不解决 Q-6**（劣化监控阈值、Paper Trading 验收标准与观察期长度放在 Profile 还是别处，
  见 ADR-0006）。本 ADR 只约束 `paper_period` 的符号，不回答它该由谁定义。
- 不新增、删除或重命名任何 Profile 字段；不改变字段的可选 / 必填状态。
- 不定义字段之间的跨字段关系（例如 `test_window` 与 `step` 的比例、embargo 与
  `available_lag` 的关系）——那些取决于校准与方法选择，不是普适结构。
- 不定义 `research_class` 的取值集合（D-09 H-7 仍开放）。
- 不实现 Profile 登记、选择或冻结流程。

## 运行时延期义务

| 义务 | 说明 |
|---|---|
| Profile 必须已 frozen | 绑定进实验的 Profile 状态检查（`06-experiment.md` §7），契约层拿不到 Profile 实例 |
| 数值是否合理 | 结构合法不等于校准合理；合理性由 Phase 4 校准报告与 Codex / Raphael 的冻结决定承担 |
| 门集合完整性 | 某个研究类别需要哪些门、Profile 是否齐备，由验证服务检查（ADR-0013 §D-20.3） |
| 版本唯一性与不可变 | 同一 `profile_id@version` 不得有两份内容，属 Control Plane 登记 |
| 选择规则的权威性 | 研究者不得自选 Profile（Constitution C-A4），由选择服务执行 |

## Schema 与迁移影响

- 受影响模型：`ValidationProfile` 及其嵌套的 `WalkForwardParams`、`DataSplitParams`、
  `CostStressParams`、`LifecycleParams`。
- 新增的是字段级范围约束，在 JSON Schema 中以 `exclusiveMinimum` / `minimum` 表达；
  `cost_model` 的 kind 校验是跨字段语义，JSON Schema 中不完全可表达，须在文档与测试中写明。
- 需要重新导出 `schemas/`；实际差异以实施时的重导出结果为准。
- `schemas/v1/` 与 `tests/vectors/v1/` **逐字节不变**。
- 不新增契约模型，`V1_MODEL_NAMES` 不变。
- 仓库内**没有**任何已登记的 Profile 实例，因此没有既有 Profile 会因本 ADR 失效。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
也没有任何 v2 数据登记。因此本 ADR 是对同一个未发布版本的收窄，
`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本矩阵是**未来实现批次**的验收条件，本轮只起草，未运行、未实现。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | `train_window` / `test_window` / `step` 为零 | 逐项拒绝 |
| 2 | 同上为负 | 逐项拒绝 |
| 3 | `sealed_oos_length` 为零 / 负 | 拒绝 |
| 4 | `embargo` 为负；为零 | 拒绝；接受 |
| 5 | `sealed_oos_max_extension` 为负；为零 | 拒绝；接受 |
| 6 | `stress_multipliers` 含 `0` / 负值 | 拒绝 |
| 7 | `stress_multipliers` 或 `reported_only_multipliers` 含 `inf` / `nan` | 拒绝（ADR-0013 §D-20.1） |
| 8 | `cost_model` 指向非 `cost_model` kind | 拒绝 |
| 9 | `paper_period` 为零 / 负 | 拒绝 |
| 10 | 结构合法的完整 Profile（数值只来自测试夹具，不进入任何文档） | 接受 |
| 11 | `_boundary_after_start` 等既有校验 | 行为不变 |
| 12 | 本 ADR 与 Constitution、roadmap、07-validation.md 的文本 | 不含任何新数值阈值 |
| 13 | `schemas/v1/` 35 份快照与 v1 固定向量 | 逐字节不变，旧哈希不变 |

## 后果

- 正面：结构上无意义的 Profile（零长度封存区、零成本压力、零观察期）无法被构造，
  Phase 4 的校准工作从一个结构已经收敛的字段集合开始。
- 负面 / 代价：测试夹具中若存在零值占位的 Profile 需要改为合法取值；
  这些夹具值只存在于测试，不得被引用为建议阈值。
- 对复现性的影响：无既有 Profile 实例受影响；v1 只读路径与旧哈希不变。

## 合规检查

- [ ] 不修改 Validation Constitution；不在任何文档中写入数值阈值（H3、ADR-0007）
- [ ] 不选择 D-09 的 TBD-1 ~ TBD-5，也不预设 H-3 ~ H-7 的答案
- [ ] 不修改 Validation Profile 的字段集合，只增加结构约束
- [ ] 不修改任何已批准 ADR 的正文
- [ ] 未实现 Profile 登记 / 选择 / 冻结流程
