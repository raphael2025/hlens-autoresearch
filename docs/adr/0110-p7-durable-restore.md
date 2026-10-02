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
