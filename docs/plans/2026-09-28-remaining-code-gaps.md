# 剩余底层代码缺口清单（2026-09-28）：交 Codex 派子代理实施

> **注（2026-09-30）**：多项已由 ADR-0098/0099/0100 完成或取代；ADR-0090 号未使用。当前状态以 `PROJECT_STATUS.md` 为准。

- **基线**：`phase/1-foundation-completion@3c957d0`。
- **来源**：6 路只读审计 GAP-1～6。覆盖全部模块、roadmap 验收项、研究库文档、ADR 中的 OPEN 项，以及生产代码没有调用点的公开 API。审计原文保存在 PM 会话中，本文件已去重、核实，并纠正了过时条目。
- **决策权**：本文件中的「PM 决定」由 Claude Code（PM）依 Raphael 2026-09-28 的授权（CLAUDE.md §0）作出。子代理只负责实现，不重新决定。标为「先写 ADR」的项，实施前先把决定写成 Accepted ADR（编号按下文），再编码。

## 0. 子代理共同规则（Codex 派工时逐字附上）

1. 先读 `CLAUDE.md`、`AGENTS.md`，再读本文件对应条目，以及条目里引用的 ADR 和文档。
2. 每个子代理使用独立 worktree 与 `feature/*` 分支，基于 `phase/1-foundation-completion` 的最新 HEAD，只在条目列出的**文件边界**内写入。边界外需要改动的地方写进报告，不要改。
3. 研究诚信规则不可让步：
   - H3：不改验证门、阈值、成本模型、数据切分、指标定义；
   - H4：不削弱测试；
   - H6：不删除失败记录。
   另外：没有任何默认数值（窗口、阈值、容量参数一律由调用方显式传入）；不写实盘或网络下单代码，不读取密钥。
4. `core/` 只允许「批 3」的单一契约任务修改，而且那个任务运行期间不能有其他任务在跑。
5. 按 D-DEBUG，本轮只写代码与测试，**不运行** pytest / ruff / mypy。若 Raphael 已开启调试，按调试规则运行，并在 `systemd-run` 内存上限下执行。
6. 交付内容：commit SHA、改动文件、已实现条款、新增测试名、OPEN 项、边界外需要改的地方。

## 1. 已完成、只需同步文档（批 0，1 个子代理）

| ID | 内容 | 文件 |
|---|---|---|
| DOC-1 | `03-data.md` §7.1 仍写着 ADR-0077 的两张新表「尚未登记」，实际已登记在 `phase1_tables.py` 第 16、17 项 | `docs/architecture/03-data.md` |
| DOC-2 | risk-library R-4 仍写「接入管线尚未授权」，实际已由 `0cfddbf` 接入 `run_with_risk` | `docs/research/risk-library.md` |
| DOC-3 | ADR-0037 manifest pairing 注记仍写「属后续批次」，实际 `ValidatorSetup.manifest_pair` 已接好（`research/strategies/validation.py`） | `docs/adr/0037-*.md`（只追加实施注记） |
| DOC-4 | outcome-library O-1 未同步，实际 `refuse_outcome_input` 已接入 `research/validation/pipeline.py` | `docs/research/outcome-library.md` |
| DOC-5 | `infrastructure/feature/dataset.py` 的 docstring 仍称「只能读私有属性，待后续」，实际公开访问器已存在（F-C） | 仅改 docstring |

## 2. 批 1：可并行，互不重叠，不碰 `core/`

| ID | 模块 | 目标 | PM 决定 | 文件边界 | Done 条件 | 规模 |
|---|---|---|---|---|---|---|
| RET-1 | P12 / Control Plane | 退役接线：`retire()` 与 `RetirementRegistry` 目前都没有生产调用点 | 仿照 `research/evolution/replacement_job.py` 新增显式退役 job。输入：退役对象、`DegradationCheck` 证据（逐份核验，与 replacement 同等强度）、具名人工 reviewer（拒绝自动化身份）。流程：调用 `retire()`，写入调用方传入的 `RetirementRegistry`。**不做自动退役**，也不接入持续循环 | 新 `research/evolution/retirement_job.py`；`tests/research/evolution/` | job 可调用；证据不全、无 reviewer、重复退役都拒绝；测试覆盖 | S |
| OPS-1 | 运维 | 各登记处的完整性核对缺少 CLI 入口 | 按 Accepted ADR-0091 新增严格只读的快照校验 API，禁止创建目录 / 锁、禁止 anchor 自动修复；链式登记处按已有格式校验，anchor 多一条时报告不一致；无可选 anchor 标为 `UNANCHORED`；Failure Registry 标为 `STRUCTURAL_ONLY`，不宣称可检测合法历史改写 | `infrastructure/registry/{registry,profile_freeze,retirement}.py`、`research/strategies/failure_registry.py`、新 `infrastructure/tools/registry_audit.py`；对应测试 | 四类都输出独立结构化结果；可证明的坏 hash / anchor / schema / 部分尾行能检出；审计不创建或写任何文件 | M |
| OPS-2 | 可观测性 | 08-deployment 承诺「stdout 结构化 JSON 日志」，目前没有实现 | 用 stdlib `logging` 加 JSON formatter，不引入新依赖。只提供模块与 `configure_logging()`；各 serve 入口的接线在批 2 完成 | 新 `infrastructure/observability/logging.py`、`__init__.py`；`tests/infrastructure/observability/` | 输出单行 JSON，字段固定；测试覆盖 | S |
| OPS-3 | apps/api | 08-deployment 写的是 `/healthz`、`/readyz`，实际只有 `/health` | 保留 `/health`，新增 `/healthz`（存活）和 `/readyz`（就绪：依赖的读取源可用），并同步更新 openapi 与 README | `apps/api/app.py`、`apps/api/openapi.json`、`apps/api/README.md`、`tests/apps/` | 三个端点均可访问；只读 | S |
| DATA-1 | P1 Representation | 成交量 bar（roadmap Phase 1 输出）尚未实现 | 规则 `hlens.canonical.volume-bar@1.0.0`，仿照 `infrastructure/canonical/resample.py`。源表 `canonical.trades`，按累计**基础资产成交量**达到调用方显式给出的阈值时收 bar，不设默认值。bar 的时间与可见时间取最后一笔成交；跨日不重置。分块读取，复用 ADR-0075 的有界扫描 | 新 `infrastructure/canonical/volume_bar.py`；`tests/infrastructure/canonical/` | 规则哈希固定；阈值必须显式给出；有界读取；精确值测试 | M |
| DATA-3 | P1 Feature | E4 派生 bar 只支持 v2 manifest | 为 `feature_request_from_derived_bars` 增加可选参数 `evidence_verifier`，改走 `load_verified_any`，做法同 C1 对 `bars/dataset.py` 的改造；v2 行为逐位不变。同时完成 DOC-5 | `infrastructure/feature/dataset.py`；`tests/infrastructure/feature/` | v3 可用；v2 不变 | M |
| DATA-4 | P1 / P11 | `SealedDatasetPair` 的 v3 分支没有测试 | 补齐 v3 的 evaluable / release / signals / binding 测试，以及 v2+v3 混配被拒绝的测试；不改产品代码 | `tests/infrastructure/e2e/test_research_loop_dataset_g5_units.py` 或同目录新文件 | 覆盖齐全 | S |
| EVT-1 | P3 Event | 现有事件 Provider 从不填写 `EventSpec.bar_spec`（ADR-0088），导致 `temporal` 组合对现有事件库一律拒绝 | 各算子的 `spec()` 增加可选透传参数 `bar_spec: Ref | None`，**不自动推导**。交互算子的上游事件 bar_spec 一致时继承，不一致即拒绝 | `plugins/events/{features,windows,states,interactions,dsl}.py`；`tests/plugins/events/` | 可以透传；交互继承并校验 | M |
| EVT-2 | P3 Event | 缺少「以另一个 Feature 为动态阈值」的算子，例如 `\|r\| > k·σ` | 新增 `feature_relative_threshold_cross(feature, level_feature, multiplier, direction)`，`multiplier` 必须显式给出；比较语义参照 `return_shock`；补内置 Manifest 和 entry point | `plugins/events/features.py`、`plugins/events/__init__.py`、`infrastructure/plugins/builtin/events.py`、`pyproject.toml`（仅 entry-points 段）、测试 | 契约套件、精确值、不看未来 | S-M |
| EVT-3 | P3 Event | 库中没有「压缩后扩张」的具体条目 | 用 `state_switch(volatility_squeeze, from=squeeze, to=expansion)` 登记为具体事件条目 | `docs/research/event-library.md`（如需要，补一个 spec 工厂到 `plugins/events/states.py`） | 条目可复现 | S |
| STATE-1 | P2 State | 没有 `state.*` 物理表与运行 artifact store（Event 已有） | **先写 ADR-0089**，仿照 ADR-0056 / 0066 登记 `state.*` 表（列、分区、只追加），提供默认无副作用、加 `--apply` 才建表的显式建表命令 | 新 `infrastructure/state/{table_definition,iceberg,store,create_state_tables}.py`；`tests/infrastructure/state/`；`docs/adr/0089-*.md` | 表定义、store、命令都存在；未在真实 Catalog 执行 | M |
| VAL-1 | P8 验证 | `verify_report` 不核验 `GateResult.value` 是否真由声明的 `metric` 算出 | **ADR-0092 Accepted**：Promotion 使用宿主组合根注入的 trusted replay Provider；绑定 report / run / experiment / Profile 并逐门比对 metric、`value` 与 `value_exact`；阈值 → 门集 → 重算检查。无 Provider、拒绝或任何输出不符时以 `report_value_not_recomputed` 失败关闭。G5 不二次开封，只能使用首次 one-shot 执行时绑定报告的可信证据；当前无内建 provider / 持久 G5 replay artifact，故当前无可用 trusted provider 时不能 Promotion。 | `research/validation/verification.py`、`research/promotion/{service.py,__init__.py}`、测试、`docs/architecture/07-validation.md` §2.1、`docs/adr/0092-*.md`、ADR 索引 | 拒绝路径有测试 | M |
| SYN-1 | P9 | `gate_calibration` 的 arm 类型写死为 `PlantedEffect`，GARCH / Jump 效应无法参与检出力评估 | arm 类型泛化为 `SyntheticEffect` 联合，并为 GARCH / Jump 定义 `arm_id` 规则（参照 `planted_arm_id`）；旧 arm 的 id 与报告字节不变 | `research/synthetic_lab/gate_calibration.py`、测试 | 新效应可以作为 arm；旧输出逐位不变 | M |
| MAT-1 | P6 矩阵 | 收益序列与状态网格不重合时，缺少 as-of 状态归属 | **先修订 ADR-0039**：归属到 `t` 之前最近一个可见状态，容忍窗口 `max_state_age` 必须显式给出、不设默认值，超出窗口视为未知状态；不改已有的「精确对齐」行为 | `research/experiments/state_strategy.py`、测试、`docs/adr/0039-*.md`（追加修订） | 可选 as-of 模式；原模式不变 | S-M |
| RETRO-1 | P8 | `retro_audit()` 没有生产调用点 | 最小 CLI：`python -m research.validation.retro_audit_cli`，显式传入 Strategy Registry 路径与 Failure Registry 路径，收集 `AuditSubject` 后调用 `retro_audit` 并写报告；Registry 为空时正常结束 | 新 `research/validation/retro_audit_cli.py`、测试 | CLI 可运行（测试用临时登记处） | S |

## 3. 批 2：依赖批 1，组内可并行

| ID | 模块 | 目标 | PM 决定 | 文件边界 | 依赖 | 规模 |
|---|---|---|---|---|---|---|
| APP-1 | P13 | `ExecutionService` 没有进程入口 | 新增 `apps/execution/serve.py`，形状同 `apps/api/serve.py`：接线 `AuditTrail(path)`、`FileEventBus`，由操作者提供 `RiskLimits` / `CostModel`（不设默认值），接入 OPS-2 日志；实盘保持关闭（ADR-0084） | `apps/execution/serve.py`、测试 | OPS-2 | M |
| APP-2 | worker | 非 loop 任务没有通用进程入口 | 新增 `apps/worker/serve.py`：可插拔 handler 注册表加 CLI，复用 `JobRunner` 的幂等与持久化结果，接入 OPS-2；不自动调度 | `apps/worker/serve.py`、测试 | OPS-2 | M |
| APP-3 | apps / reports | v3 dataset manifest、退役记录、插件发现结果都没有只读查询 | 新增 `research/reports/{dataset_manifest,retirement}.py`，只投影身份、假设、计数，不输出整表；在 `ReportKind` 中新增两个 kind，并在 `report_dto.py` 与 web 端登记；新增只读端点 `GET /plugins/{kind}`（直接调用 `discover`，失败则整体报告），配一个 web 页面 | `research/reports/`、`apps/api/{store,report_dto,app}.py`、`apps/web/src/`、测试 | RET-1（退役数据）、PLG-1 可选 | M |
| PLG-1 | 插件 | 05-plugin 中的 Plugin Registry（按 `name@version` 查找）不存在 | 新增 `infrastructure/plugins/registry.py`：接收 `discover()` 的结果，按 `name@version` 查找，不 import 具体类。**本轮不改现有组合根的直接 import**，生产接线随 P7-2 的启用决定一起做 | 新文件、测试 | — | S-M |
| RISK-1 | P5 / P11 | 研究循环和数据集路径不提供 `risk_signals`，信号型风控在循环中永远空仓 | **先修订 ADR-0049**：比照策略信号，新增风控信号的 PIT 选择通道，由循环按风险政策声明的信号引用取值；取不到时仍按 fail closed / 空仓的现有语义处理 | `research/loop/dataset_source.py`、`research/loop/stages.py`、测试、`docs/adr/0049-*.md`（追加修订） | — | M |
| RISK-2 | P5 风控库 | 风控参数空间不强制；同一 `name@version` 的不同 overrides 会互相覆盖 | 风险政策必须声明参数空间，Provider 拒绝空间外的参数点；overrides 必须产生新的版本 ref（沿用「已发布版本不可变」规则）；风控参数点计入 C-T1 的 trial family（保守计数） | `research/strategies/{volatility_target,drawdown_control}.py`、`research/strategies/_params.py`（如需要）、测试、`docs/research/risk-library.md` | — | S-M |
| DATA-2 | P1 Feature | `canonical.trades` 没有任何 Feature 输入构造器 | 新增 `trade_observations` / `feature_request_from_trades`，分块、有界读取，复用 ADR-0077 的 chunk 模式，块大小显式给出，禁止一次性全部载入内存；可以基于 DATA-1 的成交量 bar | `infrastructure/feature/observations.py`、`infrastructure/feature/dataset.py`、测试 | DATA-1、DATA-3（同文件，须在其后） | L |
| P7-2a | P7 | lowering 之后没有 Provider 执行实现 | 在 `research/hypotheses/typed_plan_providers.py` 中为已接受语义的算子实现纯 Provider（interaction、transformation 的三种、temporal、conditioning / ensemble / negation 映射到 `research/strategies/composite.py`），并建立注册表。**`TypedPlan.runnable` 仍为 False，`compile_plan` 仍拒绝**；放行留给调试之后另立 ADR | 新文件、`tests/research/hypotheses/` | — | L |

## 4. 批 3：契约 2.5.0（单一 `core/` 任务，运行期间不能有其他任务，之后是依赖它的实现）

| ID | 目标 | PM 决定 | 文件边界 |
|---|---|---|---|
| CORE-250 | 契约 2.5.0，纯 additive | **先写 ADR-0090**。① 新增 `CrossSectionalFeatureSpec`：多标的输入、截面日期网格、`rank` / `quantile` 方法与分位算法枚举、缺值处理，所有参数显式给出；② 为 G4 统计量（PBO / DSR / Sharpe 等）增加 Decimal 精确兄弟字段，规则同 ADR-0052 的 `*_exact` 与 `gate-value-quantization`。新增字段的默认值不能改变旧对象的哈希（按 ADR-0052 §4 盘点） | `core/`、`schemas/`、契约测试、`docs/architecture/02-domain.md`、`docs/adr/0090-*.md` |
| P7-1 | `transformation` 的 `rank` / `quantile` lowering 到 `CrossSectionalFeatureSpec` | 仿照 `_lower_transformation`；`runnable` 仍为 False | `research/hypotheses/` |
| VAL-2 | G4 写入 Decimal 精确值 | `robustness.py` 同时写入 `*_exact`；float 字段保留，只作展示 | `research/validation/robustness.py`、测试 |

## 5. 本轮不做（附原因，供知悉）

| 项 | 类别 | 原因 / 阻塞 |
|---|---|---|
| P11 ACTIVE / source / metric 权威解析（ADR-0080） | B | 需要真实数据源与生命周期权威 head |
| 真实联网 LLM Provider 与执行沙箱 | B | 需要网络与凭据策略，另立 ADR |
| ADR-0051 `POLICY_TABLE` 下界，以及 DQ-9 参数 | B | 调试阶段联网核实与容量实测 |
| 知识库 4 条新种子的黄金哈希 | B | 需要运行计算，调试阶段完成 |
| 知识库种子 tags / assets | B | 需要具名人工审阅 |
| P14 迁移目标与文件型 golden | B | 还没有迁移目标 |
| Docker Compose（Stage 2）、密钥管理器 | B | 部署阶段，不属于底层代码 |
| Metrics / OpenTelemetry（Stage 3） | C | 属于 Phase 4 之后 |
| 有状态出场规则（配对交易、止损） | C | 需要扩展 `StrategyProvider` 签名（H1），收益存疑 |
| 现货做空成本（ST-4） | 已定 | 维持 long_only，`negated` 在现货下拒绝 |
| HMM / 训练型状态、事件研究 CAR、meta-labeling、Kind.FACTOR、事件增量执行器 | C | 研究方向选择 |
| 分位状态的 `seed` 是否保留（S-2） | 已定 | 保留，不改 |
| Router 生命周期映射进入 `run_hash` | 已定 | 暂缓，会改变既有哈希 |
| P12-LOOP 循环内替换 | 已定 | 维持暂缓 |
| 启用 P7 运行（`runnable=True`） | 已定 | 调试通过后另立 ADR |

## 6. 建议派工顺序

批 0 → 批 1（15 项，可同时开 8–10 个子代理）→ 批 2（8 项）→ 批 3（先单独跑 CORE-250，完成后 P7-1 与 VAL-2 并行）。每批结束后由 Codex 整合到 `phase/1-foundation-completion`，再派下一批。
