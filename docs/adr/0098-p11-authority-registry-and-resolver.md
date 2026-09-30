# ADR-0098: Phase 11 生命周期权威登记处、真实 source 与 metric 解析

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-30；解除 ADR-0080 的 BLOCKED）；正文“P12-LOOP 暂缓”部分由 ADR-0100 §7 修订（循环内可选替换提案，默认关闭） |
| 日期 | 2026-09-30 |
| 决策者 | Claude Code（PM，依 Raphael 2026-09-28 正式授权，CLAUDE.md §0） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 11（Continuous Research Loop） |
| 影响范围 | `infrastructure/registry/`（新增）、`research/operations/`（新增 resolver 与 CLI 选项）；不改 `core/` |
| 是否破坏兼容 | 否（纯新增；ADR-0067 显式声明路径保留） |

## 背景（Context）

ADR-0080 因缺少三类上游权威而阻塞：(1) 可读取的当前生命周期 head / ACTIVE 集合；(2) 获准的真实数据 source 身份与冲突规则；(3) 每个可监控 metric 的精确算法。该 ADR 的“重开所需输入”列出了四项必须回答的问题。Raphael 于 2026-09-28 把开发阶段除实盘外的决策授予 PM，本 ADR 逐项回答，并把 ADR-0080 的阻塞部分改为由本 ADR 取代。

约束不变：ADR-0049 外部调度；ADR-0074 operator 仅限 synthetic；P12-LOOP 暂缓；不猜 Profile 数值（H3）；`core/` 不改（H1）；LIVE 关闭（ADR-0084）。

## 决策（Decision）

### 1. 生命周期权威：`LifecycleRegistry`

- 新增 `infrastructure/registry/lifecycle.py`：一个 append-only、哈希链接的 JSONL 登记处，沿用现有 Registry / RetirementRegistry 的文件格式、排他锁、可选外部 anchor 与只读 `verify_integrity` 模式。
- 每条记录保存一个 `core.lifecycle.strategy.LifecycleTransition`，外加策略目标 `Ref`、`prev_record_hash` 与记录序号。追加时必须传入 `expected_head`（调用方读到的 head hash）；head 不一致即拒绝（乐观并发，检测并发写入与回滚）。
- 追加前用 `core.lifecycle.strategy.validate_transition` 校验，起点状态取该策略在当前 head 的重放终态；非法转换拒绝。
- **Head 身份** = `(record_count, last_record_hash)`。**当前状态** = 按记录序号重放该策略的全部转换。**ACTIVE 集合** = 在给定 head 上重放终态为 `ACTIVE` 的全部策略。`ACTIVE → DEGRADED / RETIRED` 等后续转换自然会让该策略退出集合。
- 查询只接受显式 head，可以是最新 head，也可以是调用方钉定的历史 head；返回值同时带 head 身份。钉定的 head 若不在链上则拒绝。
- 写入者仅限显式调用（promotion / lifecycle 操作的 CLI 或函数调用）。Research Loop、ADR-0074 operator 与 API 都不得写入。

### 2. 真实 source 权威：钉定的 v3 ResearchDataset manifest

- 非 synthetic 观测的唯一 source 是 ADR-0077 v3 Dataset manifest，经 DatasetCatalog 读取。source 身份 = `(dataset_id, manifest_content_hash)`，另附 manifest 内的快照 / 政策 / 规则版本绑定。
- 该策略 promotion / validation 证据中绑定的 dataset，必须与观测 source 的 universe、bar 规格和 representation 身份一致，否则拒绝（`source_scope_mismatch`）。
- 同一 `dataset_id` 在 as-of 时刻若对应多个内容哈希不同的 manifest，则拒绝（`source_conflict`）；不自动挑选“最新”。
- 只能使用 `available_time ≤ window_end` 的数据；窗口内缺 bar 时拒绝（`source_incomplete`），不填补、不插值。

### 3. Metric 权威：与 validation 同源的闭集定义

- 新增闭集 `MonitoringMetricDefinition`（`metric_name`、`definition_id@version`、实现函数引用）。每个定义**必须直接调用** `research/validation` 中生成基线 `ValidationReport` 同名 metric 的那个函数（returns / stats 模块），在相同的收益序列构造与成本模型下计算，保证近期值与基线值同口径。
- Profile 请求的 metric 若不在闭集中，拒绝（`metric_undefined`）；不按名称自行发明均值、最新值等规则。
- 收益序列按 ADR-0049 的观测窗口构造：同一策略规格、同一成本模型、同一执行假设。回测引擎经 Provider 接口调用（H7）。每个窗口只产出一个样本；样本不足时按 validation 函数自身的 INCONCLUSIVE / 拒绝语义处理，不另设阈值。

### 4. 解析入口与 provenance

- 新增 `research/operations/authority.py`，提供 `resolve_degradation_inputs(...)`：从 `LifecycleRegistry`（显式 head）、DatasetCatalog（显式 dataset id / manifest hash）与闭集 metric 定义，产出 `run_degradation_check` 所需的 `LifecycleHistory`、`ObservationSource` / `RecentMetricManifest`。
- 同时产出 `AuthorityProvenance`：lifecycle head 身份、策略重放出的历史哈希、source 身份、metric definition id@version、as-of 时间与窗口。它写入 degradation evidence 的 authority 字段。显式声明路径不带该字段，以此区分两种证据强度。
- `degradation_cli` 新增互斥选项 `--authority-registry <root> --authority-head <hash>|latest --dataset-id --manifest-hash`。旧的显式 JSON 输入路径保留，且继续如实标注“caller-declared”。
- 任一拒绝都不写 degradation report。resolver 不调度、不循环，也不被 ADR-0074 operator 或 API 调用。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 在 StrategyRegistry 上加 lifecycle 字段 | 复用现有 journal | 混淆 artifact 与状态，改变既有记录格式 | 独立登记处更清晰，且不改既有哈希 |
| 调用方历史继续作为唯一来源 | 零实现 | ADR-0080 已论证无法证明最新性 | 保持为较弱证据路径 |
| 为监控另写一套 metric 实现 | 灵活 | 与基线口径可能漂移，触及 H3 | 必须与 validation 同源 |

## 后果（Consequences）

- 正面：P11 非 synthetic degradation 首次具备可重放、可钉定的权威输入；证据强度可以区分。
- 负面 / 代价：新增一个需要维护的登记处；首次使用前必须显式回填已有策略的生命周期记录（按真实历史逐条追加，禁止伪造）。
- 需要迁移的内容：无（纯新增）。
- 对复现性的影响：既有报告哈希不变；新报告绑定 head 与 manifest 身份，可以重放。

## 合规检查

- [x] 不破坏已冻结契约（`core/` 不改；复用 `LifecycleTransition` / `validate_transition`）
- [x] 不修改 Validation Constitution / Profile 数值；metric 与 validation 同源
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变；不引入 scheduler，不开放 API 写入

## 修订 1（2026-09-30，PM）

实现复核发现两处缺口，PM 决定如下：

1. **运行环境组装**：纯命令行无法获准地构造 DatasetCatalog、决策管线与 BacktestProvider。`degradation_cli` 新增 `--authority-environment MODULE:CALLABLE`。它指向部署方提供的**受信** factory，返回 `AuthorityEnvironment`（或产出它的 context manager），解析规则与 ADR-0095 worker `--factory` 相同：只接受标识符语法，不从消息或数据中选择代码。未提供时维持现状，以 `authority_environment_unavailable` 拒绝。
2. **回滚检测**：新增可选 `--authority-anchor <path>`，使用 LifecycleRegistry 的外部 anchor 核验 head；`--authority-head latest` 且未提供 anchor 时，evidence 中如实记录 `anchor=absent`（无法检测回滚），不因此拒绝。
3. LifecycleRegistry 纳入 ADR-0091 只读登记处审计（`infrastructure/tools/registry_audit.py`，可选 `--lifecycle-root`；未提供时审计仍为原四类登记处，报告 schema 1.1.0）。
4. degradation 基线核对按 `compare_gate` 的记录格式剥除唯一的 `[>=]` / `[<=]` 比较符后缀再比对 metric 名（缺陷修复，`09753e6`；不改变 ADR-0067 规则 5 的语义）。

## 修订 2（2026-09-30，PM）

实现审查发现 §2 的原措辞与 Canonical bar 的可用时间规则（`available_time = max(interval_end, raw.available_time + latency)`）冲突：窗口最后一根 bar 一定晚于 `window_end` 才可用，authority 模式在真实数据上永远以 `source_incomplete` 拒绝。PM 决定：

1. **评估时刻与窗口分离**：新增显式 `as_of`（CLI `--authority-as-of`，UTC），且必须 `as_of ≥ window_end`。source 只使用 `available_time ≤ as_of` 的数据，且只取 `interval_end ≤ window_end` 的 bar，这些 bar 必须恰好铺满 `[window_start, window_end)`。决策管线在回测中仍按每根 bar 自身的 `available_time` 可见（不引入前视）。§2 中“`available_time ≤ window_end`”一句据此改为“`≤ as_of`”。
2. **生命周期按时间截断**：只重放 `occurred_at ≤ as_of` 的转换；重放终态必须为 ACTIVE，且进入该 ACTIVE 状态的转换 `occurred_at ≤ window_start`，`(window_start, as_of]` 内不得有任何转换（恰好在 `window_start` 进入 ACTIVE 允许），否则 `lifecycle_not_active`。
3. **标的范围绑定**：决策管线声明的 instruments 必须等于基线 manifest 的 universe 成员集合，也必须等于 source manifest 的 universe 成员集合，否则 `source_scope_mismatch`。
4. **参数身份按规范 JSON 比较**（`canonical_json` / content hash），不使用 Python 相等（防止 `True == 1 == 1.0`）。
5. `AuthorityProvenance` 绑定 `BaselineMetricSet` 的内容哈希，`run_degradation_check` 核对该哈希。
6. **解析过程只读**：resolver 通过 LifecycleRegistry 的只读快照读取（不获取写锁，不执行 anchor 崩溃恢复写入）；anchor 只做核验，失败即拒绝。
