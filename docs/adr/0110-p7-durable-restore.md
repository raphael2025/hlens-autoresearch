# ADR-0110: P7 准入候选的持久化恢复

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，PM） |
| 日期 | 2026-10-02 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM），依据 ADR-0103 实现记录 |
| 相关 Phase | Phase 7（Dynamic Discovery）、Phase 11（Continuous Research Loop） |
| 影响范围 | `research/loop/durable.py`、`research/loop/p7_admission.py` 与相关测试；不改 `core/` |
| 是否破坏兼容 | 否（未准入 P7 计划的状态目录逐字节不变） |

## 背景（Context）

ADR-0103 让 COMMIT 后的 P7 计划进入研究循环：候选由 `P7PlanSource` 编译得到，`p7_strategy_candidates` 只接受本轮该计划的 COMMIT 证明。但持久化恢复（`research/loop/durable.py`）只会重建策略库与演化候选：准入过 P7 计划的状态目录重新打开时，以 `cannot be rebuilt: no evolution plan or no parent` 拒绝（失败关闭，有测试固定）。循环因此无法在准入 P7 后重启，P7 不能验收。

## 决策（Decision）

1. **记录来源。** 内存检查点（delta）中由 P7 准入加入的策略行，带一个显式来源段：`{"origin": "p7", "plan_hash", "round_index"}`，不带演化 `parent`。不来自 P7 的行不加任何字段，因此未准入 P7 的状态目录、`LoopRecord` 与指纹逐字节不变。
2. **重建。** 恢复时，P7 来源的行只能经由 `LoopWiring.p7_plans`（`P7PlanSource`）重建：按 `plan_hash` 取回计划，用与准入相同的编译 / 绑定路径重新生成候选，要求重建出的 `StrategySpec` 内容哈希与行中记录一致。
3. **准入证明。** 重建前必须在 plan admission 日志中找到该计划在 `round_index` 轮的 COMMIT，以及与之一致的 PREPARE；恢复把这条 COMMIT 当作 ADR-0103 所说的"本轮 COMMIT 证明"。日志中没有、或内容不一致，失败关闭。
4. **离线核对。** ADR-0073 §4 的离线核对路径（不重建对象的 `_check_memory`）对 P7 行只核对结构、`plan_hash` 与日志中的 COMMIT，不编译计划；与其他"只有重新生成才能证明"的项目一样，在报告中列为未在该路径证明。
5. **失败关闭不变。** 恢复时缺少 `p7_plans`、计划源返回不同内容、计划含横截面节点（`cross_sectional_loop_unsupported`）、或任何哈希不一致，一律拒绝打开状态目录。P7 执行开关仍默认关闭。
6. **测试。** 准入后重启得到与不重启逐字节相同的后续轮次；缺 `p7_plans`、篡改 `plan_hash`、缺 COMMIT、计划源内容变化各自拒绝；未准入 P7 的既有固定哈希不变（H4）。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 接受限制，P7 默认关闭期间不处理 | 零改动 | 循环在准入 P7 后不能重启，P7 无法验收 | 留下已知缺口 |
| 把候选的 Provider 状态序列化进检查点 | 恢复不依赖计划源 | 检查点保存可执行对象，违背"重建即证明"的恢复原则 | 降低可审计性 |

## 后果（Consequences）

- 正面：P7 准入后的循环可以恢复，满足 P11 的崩溃恢复要求。
- 负面 / 代价：恢复需要与写入时相同的 `P7PlanSource`；部署须保留计划源。
- 对复现性的影响：无；旧状态目录不变。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile / 试验计数规则（H3）
- [x] Domain 层仍无具体技术依赖
- [x] 新运行能力默认关闭

## 实现记录（2026-10-02，`feature/adr-0110-p7-restore`，实现 Agent）

按 §1–§6 落地；`core/`、Validation Constitution / Profile、试验计数均未改动，`P7ExecutionSwitch` 仍默认关闭。

- **§1 来源段**（`research/loop/durable.py`）：`_strategy_payload` 对带 `plan_record` 的候选写 `"parent": null` 与 `"provenance": {"origin": "p7", "plan_hash", "round_index"}`（键名取 `provenance`；`round_index` 为写入该检查点的轮次，即准入轮）。其他行不加字段、内容不变。
- **§2 / §3 重建与准入证明**：`open_state(..., p7_rebuild=None)` 新增可选参数（类型 `P7Rebuild`）。恢复时带 `provenance` 的行走 `_P7Proofs`：校验来源段结构、`plan_hash` 格式、`round_index` 等于本轮、`parent` 为空；在 plan admission 日志中找本轮、本计划且已有 admission checkpoint 的唯一 COMMIT（PREPARE ↔ COMMIT 由日志 reducer 绑定，PREPARE 轮次身份由 `_check_admission_checkpoints` 绑定到审计起始项）；再调用 `p7_rebuild(plan_hash, commit, round_index)`，要求重建候选的 spec 内容哈希、`plan_record.plan_hash`、family 与 risk policy 哈希等于行记录。
- **重建路径**（`research/loop/p7_admission.py`）：`P7CandidateRebuild` / `p7_rebuild(source, family_id=...)`。准入第 1 步中的编译部分抽成 `_compile`（横截面拒绝、用计划源的开关与白名单编译、策略根及其 Provider）与 `_bind`（计划记录绑定进声明的 ExperimentSpec），准入与恢复共用；恢复还要求 PREPARE 证据（compiler、operators、providers、inputs、outputs、绑定后的 experiment_specs、hypotheses）与重新编译结果逐项相等（`admission_evidence_mismatch`），最后用 `p7_strategy_candidates` 并以该 COMMIT 作为本轮证明。拒绝为 `P7RestoreRefused`（`ValueError`，带 `code`），由 durable 报为该轮检查点不一致。`compose.py` / `dataset_compose.py` 各传一行 `p7_rebuild=p7_rebuild(wiring.p7_plans, family_id=config.family_id)`。
- **§4 离线核对**：`_verify_round_offline` 对 P7 行只做结构、spec 哈希、`plan_hash` 与已检查点 COMMIT 的核对（不编译、不需要计划源）；docstring 把"计划能编译回该 spec / family / risk policy"列为该路径未证明项。
- **§5 失败关闭**：缺 `p7_plans`（`p7_rebuild=None`）、计划源未声明该计划（`plan_not_declared`）、证据不一致、开关关闭（`execution_disabled`）、横截面（`cross_sectional_loop_unsupported`）、spec / family / risk 不一致、缺少或未检查点的 COMMIT，一律 `LoopStateInconsistent`。经由组合根时，缺少或改动 `p7_plans` 先被指纹比对拒绝（`p7_plans` 已在指纹中）；`open_state` 层的拒绝由直接调用 `open_state` 的测试覆盖。
- **§6 测试**：`tests/research/loop/test_p7_restore.py`（14 项）。重启对照：同一进程跑完 3 轮作为不中断基准，并在第 0 轮结束时复制目录；复制目录重开后跑第 1–2 轮，记录哈希相同、状态目录每个文件（锁文件除外）逐字节相同。用同一次运行而非两次独立运行对照，是因为第 0 轮知识假设的 `created_at` 取墙钟（`Hypothesis` 默认值），两次独立运行本就不逐字节相同，与本 ADR 无关。`test_p7_admission.py` 中原先固定 "cannot be rebuilt" 的断言改为重开成功并核对重建候选（该场景按本 ADR 已可合法恢复），拒绝覆盖移到上述专项用例（H4）。
- **字节不变证明**：非 P7 行与 `LoopRecord` 编码路径未改；`tests/research/loop/test_loop_e2e.py` 的 `PINNED_RECORD_HASHES` / `PINNED_FINGERPRINT_HASH` 与全部持久化循环测试未改动且通过（未重钉任何值）。
- **检查（实际运行，内存上限 3G 下）**：`pytest tests/research/loop` 292 passed；`pytest tests/research/hypotheses tests/test_architecture_boundaries.py tests/test_docs_consistency.py` 266 passed；`ruff check` / `ruff format --check`（改动路径）通过；全项目 `mypy` 883 个源文件无问题。全量测试未运行。
