# ADR-0103: P7 计划绑定、准入交接与横截面执行接线

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权）；含修订 1 |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 7（Dynamic Discovery） |
| 影响范围 | `research/hypotheses/`、`research/loop/`、`research/strategies/pipeline.py`、`infrastructure/feature/`（新增横截面运行器）；不改 `core/` |
| 是否破坏兼容 | 否（新字段默认 `None`；`P7ExecutionSwitch` 默认关闭时所有记录、指纹与哈希逐字节不变） |

## 背景（Context）

ADR-0073 / 0078 / 0082 / 0088 / 0099 / 0100 已提供 lowering、10 个执行 Provider、allowlist 编译器、`TypedPlan.nodes` 输出全集权威与 PREPARE / COMMIT 日志。缺的是把它们串起来：

- ExperimentSpec / Hypothesis 与 TypedPlan 之间没有哈希绑定载体；
- 没有代码生成 inputs / outputs / experiment_specs evidence，PREPARE 只做结构校验；
- 拒绝没有持久审计落点；
- COMMIT 后的 Hypothesis 进入 TrialLedger 但从不运行，`p7_strategy_candidates` 又可绕过 admission；
- 横截面 `rank_cs` / `quantile_cs` 没有运行器与编译接线；
- ADR-0100 §1 关于 `TypedPlan.runnable` 的措辞与代码不符。

## 决策（Decision）

1. **计划绑定载体（D1）**：沿用 ADR-0100 修订 2 的 `run_inputs` 先例，在 `repro.params` 增加保留键 `hlens.p7.plan@1.0.0`，值为规范 JSON 文本：`{plan_hash, plan_format, root, compiler, allowlist_hash, universe_manifest_hashes[], nodes:[{node_id, definition, spec_ref, spec_hash, provider_key, implementation_hash}]}`。全部节点输出的 `ref → content_hash` 加入 `dependency_hashes`。Hypothesis 新增 condition `p7_plan = <64 位小写 hex>`，与 `strategy = <根策略 ref>` 并存，由 `trial_point` 解析。实现于新模块 `research/hypotheses/p7_binding.py`（`P7PlanRecord.from_compiled`、`.text()`、`with_p7_plan`、`recorded_p7_plan`；保留键被占用即拒绝）。`StrategyCandidate` 增加可选 `plan_record: P7PlanRecord | None = None`。
2. **evidence 构造**：编译器侧提供 `inputs_evidence(resolution)`、`outputs_evidence(compiled)`、`experiment_evidence(...)`；横截面节点的 universe manifest 哈希进入 evidence。PREPARE 前由组合函数交叉核对 outputs ⇔ `plan.nodes`、experiment_specs ⇔ hypotheses、operators ⇔ 编译结果（ADR-0073 §126）。
3. **拒绝审计（D2）**：复用 `StageRecord.refused` 与 `summary`，hypothesis 阶段记录 `p7_plan_rejection{plan_hash, code, where}` 与累计 `rejected_plans`。拒绝发生在任何写入之前，不登记 trial，不改 `LoopRecord` 字节格式。
4. **COMMIT → 执行交接（D3）**：新增 `LoopWiring.p7_plans: P7PlanSource | None = None`。hypothesis 阶段在本轮第一次 journal 写入之前调用 admission 组合函数（新模块 `research/loop/p7_admission.py`：编译 → 产生绑定 → 完整性校验 → 交叉核对 → 非 LLM 检查 → PREPARE → complete）；COMMIT 后把 Hypothesis 放入本轮 `registered`，origin 记为 `"p7_plan"`。`p7_strategy_candidates` 必须收到"该 plan_hash 本轮已有有效 COMMIT"的证明；`hypothesis_batch` 路径对 P7 候选拒绝。
5. **横截面执行（D4）**：新增 `infrastructure/feature/cross_sectional.py`：`run_cross_sectional_feature`（逐评估点截断）、`check_cross_sectional_answers`（含"结果成员集 = `members_at(interval_end)`"复核）、`member_values_from_source`。`OperatorImplementation` 增加 `interface: "series" | "cross_sectional"`，`CompiledPlan` 携带 `universes`，allowlist 增加两个 cs 条目，编译不再以 `cross_sectional_execution_unsupported` 拒绝。研究循环仍是单标的，含 cs 节点的计划进入循环时以 `cross_sectional_loop_unsupported` 拒绝；多标的循环另立 ADR。cs Provider 不登记 builtin manifest。
6. **运维暴露（D5）**：本批不把 P7 开关暴露给 operator TOML（会改变 ADR-0074 operator identity），只保留程序化入口。验收前不开启 P7 执行（PROJECT_STATUS §8）。
7. **勘误 ADR-0100 §1**：运行就绪由 `CompiledPlan.runnable` 表达；`TypedPlan.runnable` 保持 False（它在 payload 与哈希中，`typed_plan_audit` 依赖它）。同时修正 `research/hypotheses/dsl.py` 中 negation 的陈述（按 ADR-0088 不是验证负对照）并在 transformation 白名单加入 `rank_cs` / `quantile_cs`；这会改变**新生成**的 Hypothesis 哈希，旧记录不回填。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| `ReproducibilityTuple` 加 `plan_hash` 字段（契约 2.6.0） | 强类型 | 改冻结契约、重导 Schema、重钉哈希 | 保留键已有先例，收益有限 |
| plan_admission 日志新增 `plan_rejection` 事件 | 审计集中 | 需日志格式与状态版本升级、恢复分支 | 复用 `StageRecord` 足够且不改格式 |
| 独立 P7 stage | 隔离 | 改动审计 stage 名集合 | 侵入面更大 |
| 多标的循环 | 横截面端到端 | 推翻单标的 G5 证明 | 另立 ADR |

## 后果（Consequences）

- 正面：P7 从"零件齐全"变为可端到端准入、执行、复核；所有输出与计划哈希绑定。
- 负面 / 代价：约 1500 行代码与 2200 行测试；`trials.py` / `stages.py` / `compose.py` 是热文件，需串行合并。
- 对复现性的影响：开关关闭且 `p7_plans=None` 时零变化；旧实验无保留键，不回填（H6）。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile / 试验计数规则（H3）
- [x] Domain 层仍无具体技术依赖
- [x] 新运行能力默认关闭


## 修订 1（2026-10-02，PM，接受时）：与 phase/1 现状对齐

1. **D4 横截面执行已部分落地**：2026-10-01 在 `phase/1` 收口的 P7-CS-EXEC（ADR-0100 §1/§2）已让 `typed_plan_compiler` 在调用方显式传入 pinned universe manifest（`universes=`）时，把 `rank_cs` / `quantile_cs` 编译为专用根 Provider（`plugins/features/p7_cross_sectional.py`，含 `members_at(interval_end)` 成员集复核），并在编译期拒绝把横截面 FeatureSpec 直接作为单序列输入。因此本 ADR §5 中"新增 `infrastructure/feature/cross_sectional.py` 运行器、`OperatorImplementation.interface`、allowlist 两个 cs 条目、不再以 `cross_sectional_execution_unsupported` 拒绝"以现有实现为准，**不另建平行运行器**；只补本 ADR 仍缺的部分：横截面节点的 universe manifest 哈希进入 evidence（§2），含 cs 节点的计划进入研究循环时以 `cross_sectional_loop_unsupported` 拒绝（§5 后半）。
2. **实施顺序**：D1（`p7_binding.py`）与 §2 evidence（`p7_evidence.py`）先以审阅后的分支候选落地；随后 D1 余项（`StrategyCandidate.plan_record`、`trial_point` 解析 `p7_plan` 条件）、D2 拒绝审计、D3 准入交接（`research/loop/p7_admission.py`、`LoopWiring.p7_plans`）与 §7 勘误。`P7ExecutionSwitch` 默认关闭、`p7_plans=None` 时一切记录 / 指纹 / 哈希逐字节不变，这是硬性回归条件。
