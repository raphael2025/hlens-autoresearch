# ADR-0105: P11 运行周边补全与 D-P11-WINDOW

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权）；关闭 D-P11-WINDOW；修订 ADR-0074 §9 |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 11（Continuous Research Loop） |
| 影响范围 | `infrastructure/registry/`、`research/operations/`、`research/loop/`（新增模块）、测试；不改 `core/` |
| 是否破坏兼容 | 否 |

## 背景（Context）

ADR-0098 / 0100 的权威 resolver、`LifecycleRegistry`、指标闭集、默认 `AuthorityEnvironment` 与 CLI 已在 `main`，但：没有任何测试；Lifecycle Registry 没有生产写入端；基线制品（`ExperimentRun`、`StrategySpec`、`BaselineMetricSet` 等）没有导出工具；没有 ACTIVE 集合的批量驱动；真实数据循环没有 operator（ADR-0074 §9 排除）；D-P11-WINDOW 开放，且监控侧存在"依赖研究窗口的指标对近期窗口一律 `metric_undefined`"的问题。

## 决策（Decision）

1. **D-P11-WINDOW = 固定日历**：循环只在已冻结 Profile 的研究窗口内运行，窗口耗尽即无新研究数据，换窗口需新 Profile。监控侧明文规定：近期窗口只支持不依赖研究窗口的指标；`requires_research_window=True` 的指标对近期窗口继续 `metric_undefined`，拒绝信息须明确指出原因与本 ADR。Profile 校准时不应把这类指标列入降级阈值（由校准决定，本 ADR 不选数值）。
2. **Lifecycle 写入端**：`python -m infrastructure.registry.lifecycle_cli {append,show-head}`；`append` 需要 `--transition <json>`、`--expected-head <hash>` 与显式 anchor。写入端不得被 loop / operator / api 导入（架构测试固定）。回填只能是真实历史，不得伪造（ADR-0098）。
3. **基线导出**：
   - `research/operations/baseline_export.py`：输入显式 `ValidationReport` / `ExperimentRun` JSON 与 `--gate metric=gate_id` 映射，输出 baseline-set JSON，复用 `_check_baseline_gates` 核对；不推断映射。
   - durable 状态只读导出：在 `research/loop/` 新增只读函数，把 run / spec / cost / label 导出为文件；不改 durable 写路径与格式。
4. **批量驱动**：`research/operations/degradation_batch.py`，`run_batch(manifest: Path, *, reports_root: Path) -> BatchResult`。manifest 为显式 TOML（每行：subject、基线文件、窗口、as_of），顺序调用现有 CLI 逻辑；不发现、不选最新、不读时钟；单条失败记录后继续，汇总退出码非零。外部 scheduler 负责调用时机（ADR-0049）。
5. **Dataset 版 operator**：新增并列模块 `research/loop/dataset_operator.py` 与 `dataset_operator_config.py`，复用 `operator.py` 的状态 / 锚点检查与轮次执行（必要时把私有函数提为模块内公开函数，不改行为）。没有冻结 Profile 时拒绝一切配置（ADR-0074 规则不变）。ADR-0074 §9 的排除据此修订。
6. **证据校验器**：生产 verifier 由 ADR-0101 的 `bind_profile` 提供，部署方以三行模块指向它；`default_environment` 不改。
7. **测试**：为 resolver（生命周期、source、指标闭集、执行输入）、默认环境、CLI authority 模式、run_inputs 记录补齐测试。测试发现的源码缺陷单独修复并说明，不得削弱断言（H4）。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 滚动 Profile | 循环可持续获得新数据 | 改契约与 Profile 结构，依赖未冻结的 D-09 | 不可行 |
| operator TOML 增加 `[source] kind="dataset"` | 单一入口 | 改 1200 行配置模块与 operator 身份指纹 | 并列模块影响面更小 |
| 不提供批量驱动，只写运行手册 | 零代码 | 部署方各自重复实现 | 显式 manifest 驱动可审计 |

## 后果（Consequences）

- 正面：除部署配置（冻结 Profile、ACTIVE 策略、真实 v3 manifest、数据库）外，P11 真实运行所需代码齐备。
- 负面 / 代价：约 1500 行代码与 2500 行测试；测试可能暴露 resolver 缺陷。
- 对复现性的影响：无；报告格式不变。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile 数值，不发明指标公式（H3）
- [x] Domain 层仍无具体技术依赖
- [x] 实盘保持关闭；新运行能力需显式配置

## 实现注记（2026-10-01，P11-OPS）

已实现 §1（拒绝信息）、§2、§3 第一项、§4；§3 第二项（durable 只读导出）、§5、§7 不在本批次。

- §1：`research/operations/authority.py::_check_window_scope` 的拒绝信息现说明"该指标依赖 Profile 研究窗口"、引用 ADR-0105 / D-P11-WINDOW，并提示不应列入降级阈值；错误码仍为 `metric_undefined`，拒绝语义（哪些窗口被拒）不变。此前没有测试固定旧文本；新增 `tests/research/operations/test_window_refusal_message.py`。
- §2：`infrastructure/registry/lifecycle_cli.py`（`append` / `show-head`）。`append` 默认为 dry run（只读快照上检查头与合法性，不写任何文件），仅 `--commit` 才追加；`--transition` JSON 必须含 `occurred_at`（不读时钟）；`--expected-head` 为头记录哈希（空登记处为全零哈希），不匹配拒绝；`--anchor <path>` 与 `--no-anchor` 必选其一；已有历史旁缺失 anchor 文件时拒绝而不是新建；登记处不存在时仅 `--create` 且仅创世头可创建。退出码 0 成功 / 1 拒绝 / 3 I/O 或完整性失败。架构测试 `test_lifecycle_writer_cli_is_not_imported_by_loop_operator_or_apps` 静态固定 `research/loop/` 与 `apps/` 不导入它。
- §3 第一项：`research/operations/baseline_export.py`（见 README）。额外校验：报告须为 PASS 且与 run 的 `run_id` / `experiment_hash` 一致；指标键须是降级操作实际比较的名字（剥去一个比较符后缀）。
- §4：`research/operations/degradation_batch.py`。manifest 带 `format = "hlens.p11.degradation-batch@1.0.0"`；通过 `degradation_cli.main(argv)` 进程内调用（无 subprocess、未改 CLI 行为）；`reports_root` 只作为 `run_batch` 参数。
