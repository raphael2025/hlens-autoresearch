# ADR-0011: 生命周期主体一致性、授权有效期与时间顺序（D-17）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-24，Codex 依 Raphael 授权批准） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3） |
| 影响范围 | Lifecycle / Contract |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0006](0006-strategy-lifecycle.md)、[ADR-0008](0008-contract-payload-immutability.md)、[ADR-0010](0010-contract-construction-and-canonical-versioning.md) |

## 背景

Codex 对 `c81a548` 做 B3 只读审计，发现生命周期契约存在一组"记录了字段但没有约束"的缺口。
它们都不改变 ADR-0006 已冻结的状态机，只是把该状态机已经承诺的审计性质落成可执行校验：

1. **失败的重新验证会自动执行不可逆退役。** `core/lifecycle/strategy.py:98-106` 的
   `HUMAN_APPROVAL_TRANSITIONS` 不含 `REVALIDATION → RETIRED`，因此这条通往终态的边
   可以在无人批准的情况下完成。
2. **历史归属与时间未被校验。** `LifecycleHistory._chain_is_valid`（`strategy.py:210-220`）
   只检查状态链，不检查每条转移的 `subject` 是否属于本历史，也不检查 `occurred_at` 的顺序；
   `append`（`:197-208`）检查 subject，直接构造则不检查。
3. **授权记录可以自相矛盾。** `AuthorizationRecord`（`:146-152`）不要求
   `valid_until > authorized_at`，也不与被授权对象绑定；`RiskGateRecord`（`:137-143`）同样没有主体。
   因此一份属于另一个策略、或已经过期、或在变更之后才检查的证据，都能挂到一次 LIVE 切换上。
4. **`live_execution_enabled` 是自报字段。** `ExecutionModeChange.live_execution_enabled`
   （`:168`）由载荷构造者自己填写，却被用来判定"运行环境是否已开放实盘"。
   一个 DTO 无法证明自己的运行环境；把 Phase 13 这道红线交给载荷自证是错误的控制点。
5. **局部时间顺序缺失。** `ExperimentRun.started_at / finished_at`（`core/domain/research.py:248-249`）
   与 `RetirementRecord.active_from / active_to`（`research.py:343-344`）都可以颠倒；
   `RetirementRecord.execution_mode`（`:345`）是自由字符串，而同一概念在 `ExecutionMode` 中已有枚举。

## 精确决定

### D-17.1 `REVALIDATION → RETIRED` 必须有人类批准

`HUMAN_APPROVAL_TRANSITIONS` 增加 `(REVALIDATION, RETIRED)`。

理由必须如实表述：**失败的 revalidation report 是证据，不是执行者**。退役是不可逆终态
（`TERMINAL_STATES`），证据充分与否由人在审阅报告后判断并留下 `approved_by`。
本条**不新增也不删除任何状态机边**，只把这条既有的边纳入批准集合。

### D-17.2 LifecycleHistory 构造时的主体一致与时间单调

`LifecycleHistory` 的构造期校验补齐两项，使直接构造与 `append` 得到同样强度的检查：

| 规则 | 说明 |
|---|---|
| 主体一致 | 每条 `transitions[i].subject` 必须等于 `LifecycleHistory.subject`；不一致即拒绝 |
| 事件时间单调 | `transitions[i].occurred_at` 必须**非递减**（允许相等，不允许回退） |

只追加、可审计（ADR-0006 §3 第 4 条）要求历史是一条属于同一主体、时间不倒流的链；
时间相等允许存在，因为同一时刻的批量登记是可能的，本 ADR 不为此发明更细的排序规则。

### D-17.3 授权与 Risk Gate 的有效性和归属

| 对象 | 规则 |
|---|---|
| `AuthorizationRecord` | `valid_until > authorized_at`（零长度或倒挂的授权窗口无效） |
| `AuthorizationRecord` | 新增 `subject: Ref`，绑定被授权对象 |
| `RiskGateRecord` | 新增 `subject: Ref`，绑定被检查对象 |
| `ExecutionModeChange` → LIVE | `authorization.subject` 与 `risk_gate.subject` 都必须等于本次变更的 `subject` |
| `ExecutionModeChange` → LIVE | 授权必须覆盖变更时刻：`authorization.authorized_at ≤ occurred_at ≤ authorization.valid_until` |
| `ExecutionModeChange` → LIVE | `risk_gate.checked_at ≤ occurred_at`（Risk Gate 不得晚于它所批准的变更） |
| `ExecutionModeChange` | `from_mode != to_mode`：拒绝 no-op 变更事件 |

`risk_gate.passed` 为真的既有要求不变。

### D-17.4 删除 `live_execution_enabled`

`ExecutionModeChange.live_execution_enabled` 字段**删除**，相应的
"`live_execution_enabled` 为假即拒绝 LIVE"校验一并删除。

**DTO 只校验证据结构，不能自证运行环境。** Phase 13 与实盘开关由未来 Control Plane 的
**可信配置与授权服务**执行：该服务掌握环境事实与授权主体，在接受任何
`ExecutionModeChange` 之前判断当前环境是否允许 `LIVE`。契约层保留的是可核验的证据结构
（主体绑定、授权窗口、Risk Gate 时序），删除的是无法核验的自我声明。

**诚实边界（必须如实表述）**：本 ADR 实施后，契约层**不再**拒绝 `to_mode = LIVE`。
"Phase 13 之前禁止真实生产交易"（ADR-0006 §3 第 5 条、H10）依然有效，但它的执行点
在 Control Plane 与人类授权，不在 DTO。不得把本条描述为"放宽了实盘限制"，
也不得把删除前的自报字段描述为曾经提供过真实防护。

### D-17.5 Run 与退役记录的本地时间顺序及枚举

| 对象 | 规则 |
|---|---|
| `ExperimentRun` | 两者都存在时 `started_at ≤ finished_at` |
| `RetirementRecord` | 两者都存在时 `active_from ≤ active_to` |
| `RetirementRecord.execution_mode` | 类型由 `str \| None` 改为 `ExecutionMode \| None` |

这些是**记录内部**的一致性，不是跨对象的时间线校验。

## 明确不做

- **不解决 Q-4 / Q-5 / Q-6 / Q-7**（ADR-0005、ADR-0006 的开放细节）。D-17.1 只针对
  `REVALIDATION → RETIRED` 这一条边，不重新审视 Q-4 的批准点集合，也不回答
  Q-5"人工是否可以直接执行退役"的一般性问题。
- **不增删状态机的任何边**，不新增状态，不改变终态集合。
- 不引入 `LIVE` 生命周期状态（C-2 已定）。
- 不定义 Risk Gate 的检查内容、风险预算的单位或授权的组织流程。
- 不跨对象校验时间线（例如 Run 的时间与生命周期转移时间的关系）。
- 不实现 Control Plane、授权服务、审计日志或任何持久化。

## 运行时延期义务

以下属于未来 Control Plane / Registry / 授权服务，本 ADR **不实现**，也**不引入任何自报布尔标志**：

| 义务 | 说明 |
|---|---|
| 实盘开关 | 当前环境是否允许 `LIVE`，由可信配置决定，不由载荷声明 |
| 授权主体真实性 | `authorized_by` 是否为有权批准的人、`approved_by` 是否有效，由授权服务核验 |
| 证据引用可取回 | `LifecycleTransition.evidence` 指向的报告是否存在、是否与结论一致 |
| 跨对象时间线 | 转移时间与 Run、报告、监控事件之间的全局因果顺序 |
| 唯一当前状态 | 同一 subject 是否只有一条权威历史（契约层拿不到账本） |

## Schema 与迁移影响

- 受影响模型：`LifecycleHistory`、`ExecutionModeChange`、`AuthorizationRecord`、
  `RiskGateRecord`、`ExperimentRun`、`RetirementRecord`。
- `AuthorizationRecord` 与 `RiskGateRecord` 新增必填 `subject`；`ExecutionModeChange`
  删除一个字段。两者都会改变对应模型的 `content_hash`，也会改变导出的 JSON Schema。
- 需要重新导出 `schemas/`；实际差异以实施时的重导出结果为准。
- `schemas/v1/`（35 份）与 `tests/vectors/v1/` **逐字节不变**；v1 与 v2 的内容哈希本就不可比较。
- 不新增契约模型，`V1_MODEL_NAMES` 不变。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
仓库内也没有任何 v2 数据登记。因此本 ADR 是对同一个未发布版本的收窄，不是新的破坏性变更，
`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本 ADR 已获批准；下列矩阵是**实现批次**的验收条件。实现尚未发生，矩阵未运行。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | `REVALIDATION → RETIRED` 且 `approved_by` 为空 | 拒绝 |
| 2 | 同上但有 `approved_by` | 接受 |
| 3 | `LifecycleHistory` 直接构造，其中一条转移的 `subject` 属于别的对象 | 拒绝 |
| 4 | `LifecycleHistory` 的 `occurred_at` 回退 | 拒绝；相等时接受 |
| 5 | `AuthorizationRecord` 的 `valid_until == authorized_at` / 早于 `authorized_at` | 两者都拒绝 |
| 6 | 切 LIVE 时 `authorization.subject` 或 `risk_gate.subject` 与变更主体不一致 | 拒绝 |
| 7 | 切 LIVE 时 `occurred_at` 晚于 `valid_until`，或早于 `authorized_at` | 拒绝 |
| 8 | 切 LIVE 时 `risk_gate.checked_at` 晚于 `occurred_at` | 拒绝 |
| 9 | `from_mode == to_mode` 的变更事件 | 拒绝 |
| 10 | 载荷中仍出现 `live_execution_enabled` | 拒绝（`extra="forbid"`） |
| 11 | `ExperimentRun` 的 `finished_at` 早于 `started_at`；只有其一时 | 前者拒绝；后者接受 |
| 12 | `RetirementRecord` 的 `active_to` 早于 `active_from` | 拒绝 |
| 13 | `RetirementRecord.execution_mode` 传非枚举字符串 | 拒绝 |
| 14 | 状态机转移集合与 ADR-0006 图逐条比对 | 边的集合不变 |
| 15 | `schemas/v1/` 35 份快照与 v1 固定向量 | 逐字节不变，旧哈希不变 |

## 后果

- 正面：不可逆退役需要人的签字；历史不能混入别的主体或时间倒流的记录；LIVE 证据必须
  属于同一主体、在授权窗口内、且 Risk Gate 先于变更；无法核验的自证字段被移除。
- 负面 / 代价：`AuthorizationRecord` 与 `RiskGateRecord` 增加必填字段，构造方需要提供主体；
  现有测试工厂需同步；删除 `live_execution_enabled` 后，契约层不再是 Phase 13 红线的执行点，
  该职责必须在 Control Plane 落地之前保持在人与流程上。
- 对复现性的影响：v1 只读路径与旧哈希不受影响；仓库内无生命周期历史数据受影响。

## 合规检查

- [ ] 不修改 ADR-0006 已冻结的状态机（只调整批准集合，不增删边）
- [ ] 不修改 Validation Constitution 与 roadmap
- [ ] 不修改任何已批准 ADR 的正文
- [ ] Domain 层仍只依赖标准库与 Pydantic
- [ ] 未实现 Control Plane / 授权服务 / 存储；未引入自报布尔标志
