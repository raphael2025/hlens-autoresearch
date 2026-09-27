# ADR-0074：Synthetic Research Loop 本机有限批次 Operator

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-28 |
| 决策者 | Codex（依 Raphael 于 2026-09-28 的项目全权委托） |
| 相关 Phase | Phase 11（synthetic-only operator） |
| 影响范围 | Research / Application boundary、Worker operation、durable state、reports |
| 是否破坏兼容 | 否（不改公开 Domain Contract / Schema）；研究状态目录内部配置指纹需要升版，见决策 §6 |

## 背景（Context）

Phase 11 已有通用调度器、预算、审计、事件总线和持久研究组合，但缺少人可直接启动的正式入口。`ResearchLoop.run_unattended(rounds)` 已支持一次执行有限的正整数轮数；研究侧的 `open_synthetic_loop` 可打开并恢复同一 durable state。两者目前没有命令行组合入口。

职责边界已经确定：`apps/worker` 只依赖 `core` 和标准库；研究阶段和组合根留在 `research/loop`，依赖方向为 `research → apps/worker`（ADR-0049）。`apps/api` 是只读查询面；不提交、不触发、不控制 worker 作业（ADR-0048）。本 ADR 只为本机的合成研究路径定义有限批次 operator，不把合成结果当作真实市场证据（ADR-0042）。

本提案沿用现有 `SyntheticLoopConfig` 和 `LoopWiring` 的字段，不为方便 CLI 而改变这些类型。operator 负责解析严格配置、从封闭 provider allowlist 创建对象、打开持久组合、有限运行并输出可查询的报告。现有 state fingerprint 未覆盖全部运行绑定（例如 provider descriptor 身份及 `ProfileSelection`）；operator 不能声称仅靠当前 fingerprint 就能阻止所有不一致的重开。

## 决策（Decision）

### 1. 入口与每次运行的边界

1. 唯一首期入口为研究侧模块：

   ```text
   python -m research.loop.operator run --config PATH --rounds N
   ```

   `PATH` 必填；`N` 必填且为正整数。无 `--rounds` 默认值、无无限循环模式、无后台 daemon 模式。入口调用 `research.loop.compose.open_synthetic_loop(...)`，然后至多追加 N 轮。每轮运行后立即处理该轮可恢复报告，再开始本次的下一轮。

2. 持续运行由外部 scheduler 完成：scheduler 重复启动同一 operator，传入同一配置、`loop_id` 和 `state_dir`。每次进程只完成有界批次并退出。配置中的 `epoch` / `cadence` 是研究轮次的逻辑计划时钟，不是 scheduler 的启动间隔。

3. operator 位于 `research/loop`。不得在 `apps/worker` 中加入对 `research/` 的 import，不得以 worker CLI、API 写端点或隐藏的服务回调绕开 ADR-0049 / ADR-0048。

### 2. 严格、版本化 TOML 与现有配置对象

1. 配置是 UTF-8 TOML，顶层必含 `schema_version = "1.0.0"`。首期用 Python 标准库 `tomllib` 解析。每张表拒绝未知键、重复键、缺失必填键和错误类型；不做环境变量插值、配置合并、隐式搜索路径或“尽力猜测”。升级配置结构时提升配置 schema version；不支持的版本立即拒绝。当前没有冻结的 production Validation Profile，因此实现阶段 README 只能提供字段齐全但带显式占位符的**不可运行模板**，并说明 parser 会拒绝占位符；只有未来存在真实冻结 Profile 后才能增加可运行样例。不得用 TEST ONLY Profile、临时数值或默认值伪造合法配置。

2. 固定 TOML 结构为：顶层 `schema_version`；`[paths]`；`[operator]`；`[providers.<role>]`（角色只允许 `synthetic_market_provider`、`feature_provider`、`state_provider`、`strategy_provider`、`backtester`、`outcome_provider`）；`[loop]` 对应 `SyntheticLoopConfig`；`[loop.wiring]` 对应 `LoopWiring`。`[providers.<role>]` 只保存该 role 的 allowlist `id`、`version`、`descriptor_hash`，由编译器注入相应现有 provider 对象字段；它不是新的 domain/dataclass 字段。`[operator]` 只允许 `llm_enabled = false`。所有多层未知表和未知键都拒绝。复杂对象字段如 `profile`、`profile_selection` 和 spec 字段用 `{ path, content_hash }` 作为严格 artifact reference；剩余配置值直接使用现有 dataclass 字段名。路径根于配置文件所在目录。

   ```toml
   schema_version = "1.0.0"

   [operator]
   llm_enabled = false

   [paths]
   state_dir = "state/loop-a"
   state_anchor = "anchors/loop-a-state.jsonl"
   bus_anchor = "anchors/loop-a-bus.jsonl"
   reports_root = "reports"

   [loop]
   loop_id = "loop-a"
   seed = 1
   epoch = "2026-09-28T00:00:00Z"
   cadence = "86400 seconds"
   llm_prompt = ""
   llm_cost_units_per_call = "0"
   # Other existing SyntheticLoopConfig fields are required here.

   [loop.wiring]
   # Existing required LoopWiring fields are required here.
   evolution = false # compiler maps this explicit disable to evolution = None
   oos_unseal = false
   sealed_decision_step = false
   conditional = false
   hypothesis_batch = false
   knowledge_source = false

  [providers.synthetic_market_provider]
  id = "hlens_synthetic_random_walk"
  version = "1.0.0"
  descriptor_hash = "<required exact content hash>"

   [providers.strategy_provider]
   id = "research_tsmom"
   version = "0.1.0"
   descriptor_hash = "<required exact content hash>"
   ```

   TOML 没有 `null` 值。示例仅说明层级，不是完整可运行配置；实现须在 README 提供所有 required values 的合法样例。布尔 `false` 是 operator 的显式禁用语法：对 `evolution` 映射为其 required nullable field 的 `None`；对五个 optional wiring field 映射为 `None`；在 `llm_enabled=false` 时把空 `llm_prompt` 映射为 Python `None`。该语法不向 domain/dataclass 增加布尔开关。

3. 配置编译器必须构造一个且仅一个 `SyntheticLoopConfig`，并填充其每个字段：
   `loop_id`、`seed`、`epoch`、`cadence`、`budget`、`market`、`minutes_per_round`、`compute_seconds_per_bar`、`wiring`、`family_id`、`knowledge`、`max_new_hypotheses_per_round`、`max_reevaluations_per_round`、`hypothesis_compute_seconds`、`compute_seconds_per_trial`、`validation_compute_seconds`、`state_compute_seconds`、`profile`、`constitution_version`、`llm_prompt`、`llm_cost_units_per_call`。

4. `wiring` 必须映射 `LoopWiring` 的真实字段：`feature_provider`、`feature_spec`、`feature_chunk_bars`、`state_provider`、`state_spec`、`decision_step`、`decision_warmup`、`strategies`、`backtester`、`cost_model`、`initial_equity`、`outcome_provider`、`label_spec`、`robustness`、`profile_selection`、`declared_research_class`、`code_commit`、`environment_lock`、`evolution`、`oos_unseal`、`sealed_decision_step`、`conditional`、`hypothesis_batch`、`knowledge_source`。前 19 个 required 字段（含 `evolution`）均要求提供；后 5 个 optional 字段也必须显式配置为 `false`（编译为 `None`）或完整的受支持值。首期仅 `evolution = false`、`oos_unseal = false`、`sealed_decision_step = false`、`conditional = false`、`hypothesis_batch = false`、`knowledge_source = false`；其中 `evolution` 是当前 `LoopWiring` 的 required nullable value，而非 optional dataclass field。

5. `llm_prompt` 和 `llm_cost_units_per_call` 在现有 `SyntheticLoopConfig` 中虽有 Python 默认值，配置文件仍须显式表达：首期配置 `llm_prompt = ""`、`llm_cost_units_per_call = "0"`，且显式 `[operator] llm_enabled = false`；编译器在验证此状态后把空 prompt 映射为 Python `None`。首期拒绝启用 LLM；不得从 provider 环境变量或本地凭据自动启用。

6. 复杂契约对象以配置内的明确文件引用提供：引用必须指定相对配置文件目录解析的路径及对象的预期 `content_hash`；解析后用该对象的现有 Pydantic / Contract 类型校验，并逐字核对内容身份。具体用于 `profile`、`profile_selection`、`feature_spec`、`state_spec`、`strategies`、`cost_model`、`label_spec`、`knowledge` 等现有类型；不能仅凭 TOML 中的 `name` 或版本字符串替代内容哈希。`SyntheticMarketSpec` 本身的字段按其契约提供，或作为同样带哈希的对象引用读取。

7. 所有时间必须显式带时区并规范化为 UTC；`epoch` 与合成市场 `start` 不是本机时区时间。每个 `timedelta` 在 TOML 中使用唯一格式 `"<finite decimal> seconds"`，最多六位小数，编译为精确微秒；不得由 bare integer 猜单位。所有 `Decimal` 配置值必须以字符串写入 TOML，禁止 TOML float；拒绝 NaN、Infinity、布尔冒充整数及不符合目标契约范围的数值。`LoopBudget` 四项均必填：`max_trials_per_round`、`max_trials_total`、`max_llm_cost_units`、`max_compute_seconds`，不提供 operator 默认预算。所有 `SyntheticLoopConfig` 和 `LoopWiring` 的计数、节奏、compute 声明、G4 `robustness` 参数均按其现有字段显式提供，不因配置缺省而填测试值。

8. `profile` 必须是内容哈希匹配、状态为 `FROZEN` 且在 ADR-0062 `ProfileFreezeRegistry` 中有匹配冻结记录的非 TEST ONLY Profile；`profile_selection` 及其对应规则也必须完整校验。不得以 `status = FROZEN` 字段单独代替冻结登记。当前冻结登记为空且 Profile 数值尚未冻结（ADR-0062、`PROJECT_STATUS.md`），因此本 ADR 获批后仍不能立即提供可运行的合规配置；operator 在首个有效冻结 Profile 登记前必须拒绝所有运行配置，不得用测试夹具或占位 Profile 填补。

### 3. 静态 provider allowlist

1. operator 内维护按 provider role 分类的静态 allowlist；配置必须逐项指定已注册 provider 的稳定 ID、版本和 descriptor 的精确 `content_hash()`。启动时创建 provider，重算 descriptor 并逐字段核对 ID / version / hash。未知 ID、同 role 重复注册、descriptor 不匹配、provider 声称支持的 spec 与实际 spec 不一致时，在打开 state 目录或运行任何阶段前拒绝。

2. 首期仅允许下列现有确定性本机实现，并且每个 role 只注册这一个 v1 实现：
   - `SyntheticMarketProvider`: `plugins.synthetic.random_walk.RandomWalkMarket`，descriptor `hlens_synthetic_random_walk@1.0.0`；
   - `FeatureProvider`: `plugins.features.bars.BarLogReturnProvider`；
   - `StateProvider`: `plugins.states.regimes.TrendRangeProvider`；
   - `StrategyProvider`: 首期仅 allowlist `research.strategies.time_series_momentum.TimeSeriesMomentumProvider`；策略 `StrategySpec` 仍须单独作为 `LoopWiring.strategies` 的版本化 hash-bound 输入；首期不注册 cross-sectional strategy 或 risk provider，且不得打开 Evolution；
   - `BacktestProvider`: `plugins.backtest.bar.BarBacktester`，只用其模拟执行模式；
   - `OutcomeProvider`: `plugins.outcomes.forward_return.ForwardReturnOutcome`。

   Allowlist 的每个条目必须固定 descriptor 的精确版本/hash，并接受唯一、显式的 spec/constructor 参数。不得扫描插件目录、读取 Python entry points、接受 `module:factory`、调用用户配置的导入路径或任意 Python 工厂；provider 创建不允许任意代码执行。

3. `code_commit` 必须等于当前受信工作树实际 Git commit；`environment_lock` 必须绑定当前锁文件内容。两个值都写入配置和研究复现绑定；不能接受 `test-only` 标记、虚构 SHA 或缺失值。Strategy、Profile、ProfileSelection、Knowledge、Feature/State/Outcome/Cost specs 均需精确版本和内容身份。

### 4. 路径、持久状态和外部锚点

1. 配置中 `[paths]` 必须显式提供 `state_dir`、`state_anchor`、`bus_anchor`、`reports_root` 四个不同路径。相对路径只相对 TOML 文件所在目录解析，之后统一规范化为绝对路径。路径解析后如果路径相同、彼此包含而违反以下要求、或锚点落入 state directory，均拒绝。

2. `state_dir` 是唯一 loop durable state 目录；首次运行可不存在或为空，重开时必须由 `open_synthetic_loop` 按现有结构和配置完整校验。非空但不是该 operator 创建的合法 state、与其他 loop 身份/配置不符、被其他 writer 持锁或存在未记录的中断轮次时均 fail closed。不得自动清空、迁移、修复或换目录续跑。

3. `state_anchor` 为 `state_dir` 之外的 `FileAnchor` 文件；`bus_anchor` 为 `state_dir` 和 `state_dir/bus` 之外的 bus `FileEventBus` anchor 文件。首期要求二者都启用，路径不得与 `state_dir`、彼此或 `reports_root` 重合，也不得相互包含。该要求检测保留锚点时的本机状态回滚/尾部删除；本机文件锚点不是签名、不是外部可信存储，也无法抵抗同时回滚或篡改数据目录和两个锚点的行为。ADR-0049 的锚点诚实边界仍适用。

4. `reports_root` 必须与 state directory 和两个锚点互相独立；API 的 `create_app(reports_root=...)` 必须指向同一个目录。报告写入仅在 research 侧执行；operator 不启动 API、不改变 API 权限，也不新增 API 写入或触发端点。

### 5. 配置身份、预算与 scheduler 重入

1. 重开必须精确绑定有效运行配置。现有 `settings_fingerprint` 继续作为基础，但首期实施前须扩展 operator 使用的 durable identity，使其还覆盖：TOML schema version；provider role/id/version/descriptor hash；`ProfileSelection` 内容哈希；所有有效的 `SyntheticLoopConfig` / `LoopWiring` 字段及明确禁用项的规范化内容哈希。`state_dir`、anchor / bus anchor、reports path、命令行本批 `rounds` 不改变实验语义，不进入此研究配置身份；路径由 operator 启动校验。每个 scheduler invocation 仍必须提交完全相同的语义配置。

2. ADR-0073 已决定 P7 plan-admission state format 从 v3 升到 v4，并要求 v3 / v4 明确分支兼容。operator identity 进一步改变 header 身份，因此 operator 专用 state format 必须在 ADR-0073 的 v4 基础上升到 **v5**；不得把它笼统写成“下一个版本”。v5 保留 v3 既有 loop 格式和 v4 plan-admission 格式的旧格式分支，但 operator 只创建 / 打开 v5 state。现存 v3、v4 目录都不得由 operator 接管、原地迁移或伪装成 v5；它们仍由原格式实现读取，或在另立、审阅过的迁移方案后处理。v5 identity 在 state header 中 fsync 后不可改变。该内部版本变更不更改 `core/contracts`、导出 Schema 或 `LoopRecord` hash。

3. `LoopBudget` 和本批轮数职责不同：预算是该 loop 生命周期硬上限，`--rounds N` 只是本次启动的最大新增轮数。预算耗尽、用量超出声明或其他现有 halt 状态都写现有 `LoopRecord` 并保持停机；CLI 只汇报，绝不扩大预算、清零累计量或自动换 state。变更预算或任一绑定配置需要一个新 `loop_id` 和全新 `state_dir`。同一 scheduler job 不能并发执行；现有 state lock / bus lock 冲突时直接拒绝并报告。

4. scheduler 仅调用上述一次性命令，例如显式配置的 systemd timer 或 cron；本 ADR 不新增 scheduler 依赖或后台服务。systemd/cron 频率、重试策略、运行用户与 `--rounds` 由 operator 使用方显式配置。scheduler 重试只允许重新调用完全相同配置；如果 state 已留下 `started` 未 `recorded`，下次调用将按本 ADR §7 拒绝，不把“重试”解释为自动重跑。

### 6. durable identity 的实现决策

当前 `settings_fingerprint` 已绑定 `LoopBudget`、profile、knowledge、策略/spec 内容、代码 commit、environment lock、节奏、G4 参数和 market identity，但不完整覆盖 provider descriptor、`ProfileSelection`、所有阶段 compute 声明或 TOML schema。只靠 `code_commit` / `environment_lock` 也不足以表达用户在同一二进制 allowlist 中选取的 provider 身份。

为满足 §5 的重开边界，operator 编码前先完成 ADR-0073 已批准的 v4 plan-admission durable format；随后新增研究侧组合参数 `operator_identity`，其内容是 §5 所列语义配置规范表示的 SHA-256，由 `open_synthetic_loop` 与 durable state 一同校验，组成 operator-only `STATE_VERSION = 5`。它不是 `SyntheticLoopConfig` 或 `LoopWiring` 的新字段，也不是 Domain Contract。v5 plan journal header 使用同一 journal schema `1.0.0`，并精确绑定 `state_version: 5`；v4 header 始终精确绑定 `state_version: 4`。实现顺序固定为：先实现并保留 v3 / v4 opener 分支及 v4 admission 语义，再在其上实现 v5 operator identity；不得把 v4 格式跳过、重命名或并入 v5，也不得在尚无 v4 基线时实现 operator state header。

### 7. 报告交付、恢复和中断

1. 首期只保证 `research_loop_round` 报告。每次启动、打开并通过 durable state 校验后，先遍历 `loop.audit.records` 的全部已验证记录，以 `write_research_loop_rounds(reports_root, records)` 幂等补齐报告；随后按一轮一个调用执行 `loop.run_unattended(1)` 并立即写出新记录。报告 id 使用现有 `LoopRecord.record_hash`，writer 冲突或 malformed state 立即停止。重开补报保证 audit fsync 后、report 写入前进程崩溃时，下一次健康调用可补齐轮次报告。

2. **Phase 6 `state_strategy_matrix` 报告不属于首期交付。** 现有 `run_unattended_and_report` 在整批运行完后才写 P6 matrix 和 round reports；audit 没有保存可恢复的完整 `StateStrategyMatrix` 对象，不能从历史 `LoopRecord` 证明重建它。首期 operator 不调用这个批次后置 sink，不宣称矩阵报告可恢复；matrix API 在此 operator 下可以为空。后续若要包含矩阵报告，需单独设计与验证逐轮 durable outbox/checkpoint 语义，不得用 report 文件存在与否推断实验是否运行。

3. 正常 SIGINT / SIGTERM 只请求在当前 round 完成并记录、锚定和写出其 round report 后退出；处理器不得在阶段中途伪造正常结束。强杀、掉电或未捕获故障若发生在 `loop_round_started` 之后、完整 round checkpoint/audit 记录之前，按 ADR-0070 / ADR-0049 fail-stop：同目录禁止自动续跑/重试；operator 以 recovery-required 错误退出，人工审阅后使用隔离的新 loop/state，旧审计、Ledger 和失败证据保留。若 round audit 已持久化而 operator 在报告补齐前崩溃，下一次成功启动可以从已验证 audit 幂等重写 round report。

### 8. 配置拒绝与退出码

operator 必须在运行阶段前拒绝：

- 未知配置键、重复 TOML 键、缺字段、schema version 不支持、配置引用丢失/哈希不符、不可解析或非法的契约对象；
- `--config` 或 `--rounds` 缺失，轮数非正整数；不是 UTC 的 `epoch` / `market.start`，非正 cadence 或违反现有类型约束的数值；
- 缺失任一 budget、计算用量声明、profile、有效 ProfileFreezeRegistry 冻结记录、ProfileSelection、策略/知识输入、Provider identity、代码版本或环境锁；
- Provider 不在静态 allowlist、descriptor/hash/version/spec 不精确相符、任何动态 import / Python 工厂要求；
- `llm_enabled != false`、空 `llm_prompt` / `"0"` LLM cost 与 disabled 状态不一致、首期六个显式 disabled wiring mode 中任一个不为 `false`，或 SyntheticMarketSpec 之外的数据源/真实数据引用；
- TEST ONLY profile、参数、provider、environment lock；D-LIST 或真实市场数据的声明被包装成合成证据；
- `state_dir` 身份/fingerprint 不一致、锚点缺失/重合/在 state 目录内、reports path 与上述路径重叠、state/bus 已锁定、损坏/未知的 state 或发现未记录的 started round。

退出码固定为：`0` 本批 N 轮已执行并完成报告（或收到停止信号后在 round 边界正常结束）；`2` 命令/配置/provider/identity 拒绝且没有运行阶段；`3` loop 已按预算或现有 halt 状态停止，完整记录已保留；`4` 中断轮或 ADR-0070 recovery-required，必须人工审阅；`5` state/bus/anchor/report I/O、损坏或校验失败。stdout 输出 loop id、本次新增轮数、各 round hash/status、停止或恢复状态及报告路径；不得输出密钥或环境凭据。

### 9. 明确排除

此 operator 只运行 ADR-0042 synthetic provider 路径，不接 `DatasetLoopConfig` / Research Dataset，不接 Binance、交易密钥、账户、订单、纸面或 live execution，不产生真实市场结论，不做生命周期人工审批，不启动 Web/API。该决定不触及 D-LIST / ADR-0051，不修改 Constitution、Validation Profile 数值、任何 `core/contracts` 或发布 Schema，不增加 live trading 能力。

Raphael 于 2026-09-28 将项目整体决策与执行权委托给 Codex。Codex 审阅后接受本 ADR，作为后续 operator 实现的范围基线。当前没有已冻结的可运行 Validation Profile，因此接受本 ADR 不会生成可运行配置，也不允许用测试 Profile 或临时数值代替；实现依赖 ADR-0073 的 v4 durable admission 先行完成。

实现细节补充（2026-09-28）：`operator_identity` 只覆盖 §5 明确列出的语义配置，不含 state / anchor / bus / reports 路径或本次 `--rounds`；operator 专属 durable state 固定为 v5，v5 plan journal header 绑定 `state_version: 5`。由于当前无冻结的 production Profile，README 只提供会被 parser 拒绝的完整占位符模板；不要求也不允许制造当前可运行配置。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 研究侧严格 TOML + 有限批次 + 静态 allowlist（本提案） | operator 可重复调用；配置和依赖可审阅；沿用研究组合根和持久状态；不越 API / Worker 边界 | 需要编写严格 TOML 编译器、allowlist 和 identity 扩展；不支持任意插件与 P6 report recovery | 推荐；把第一条正式运行路径限制为可以明确复现、恢复和拒绝的 synthetic-only 功能 |
| B. 直接执行 `module:factory` / Python 配置工厂 | 参数表达灵活、初期实现少 | 配置等于任意代码执行；模块导入路径和闭包状态难以哈希/重现；无法给出封闭 allowlist | 拒绝；不满足静态 Provider 注册与可审阅运行配置 |
| C. API POST trigger 或常驻无限 worker | UI 可发起运行或自动保持在线 | 改变 ADR-0048 只读 API 边界；无限 daemon 的退出/重入与预算/中断恢复需要额外架构 | 拒绝；本期由 operator CLI 启动，API 仅查询 |

## 后果（Consequences）

- 正面：具备一条可通过 scheduler 重复启动的本机研究执行路径；每次有界运行；配置、provider、预算、durable state 与 report 之间的关联可检查；合成结果与真实市场证据隔离。
- 负面 / 代价：strict TOML 编译器需维护 `SyntheticLoopConfig` / `LoopWiring` 映射；state identity 需增加规范化 operator hash 并提升内部目录版本；外部锚点需单独保留；operator v1 不提供 P6 Matrix 报告、数据集路径、LLM 或任意插件。
- 需要迁移的内容：公开 Contract / Schema 无迁移。按 ADR-0073 先实现 state v4（保留 v3），再实现 operator-only v5（保留 v3 / v4）；operator 拒绝直接接管 v3 / v4 目录。任何数据迁移须另立方案并批准。
- 对复现性的影响：旧 LoopRecord 和其 hash 规则保持不变；operator 配置的规范 hash 绑定当前实验身份。旧实验仍由原始代码 / state format 复现，不能用修改后的配置静默续跑。

## 合规检查

- [x] 不更改冻结 Contract 或 Schema；internal durable-state format 版本升级明确列出
- [x] 不修改 Validation Constitution / Profile / 数值阈值
- [x] Domain 层不增加技术依赖
- [x] Research / Application Plane 边界不变；研究 operator 只读出 API 查询所需的 reports
- [x] 不加入实盘、账户、密钥、订单或交易触发能力
- [x] Codex 依 Raphael 2026-09-28 项目全权委托接受；实现仍须等待 ADR-0073 v4 durable admission 完成

## 实施边界（后续实现任务）

1. `research/loop/operator_config.py`：TOML v1 严格解析、path resolution、强类型校验、hash-bound artifact loading；只接受 ADR §2 定义对象。
2. `research/loop/operator_providers.py`：实现 ADR §3 的静态 role allowlist 和 descriptor/spec identity 校验；不动态导入。
3. `research/loop/operator.py`：argparse `run` 命令；配置到现有 `SyntheticLoopConfig` / `LoopWiring` 的单向组装；校验 anchors/locks；durable open；audit round-report catch-up；每轮执行与 graceful stop；固定退出码。
4. `research/loop/compose.py` / `research/loop/durable.py`：先按 ADR-0073 完成 v4 plan-admission state format，再按本 ADR §6 加入 operator identity 并实现 v5 opener；保留 v3 / v4 分支且禁止 operator 接管旧目录。不扩展公共 Domain Contract。两个批次必须分开提交，第二批不可先于 v4。
5. `research/loop/README.md`：写配置字段、allowlist、运行/外部 scheduler 样例、拒绝语义、报告限制与恢复手册；不要把本地 test fixture 值当作 production profile。

依赖只用 Python 3.13 标准库 `tomllib` 与 `argparse`，不新增 pip dependency。仓库当前以 `python -m` 作为可用形式；若未来需要安装式 console script，应另行确认 packaging（当前 `pyproject.toml` 关闭 package build）。

## 参考

- [ADR-0042：SyntheticMarketProvider](0042-synthetic-market-provider.md)：随机游走 provider 只用于方法校准，不支持真实市场结论。
- [ADR-0044：EventBusAdapter 与 worker jobs](0044-event-bus-and-worker-jobs.md)：通用 worker 依赖边界、持久总线与任务行为。
- [ADR-0048：API 服务与研究控制台](0048-api-and-web-console.md)：API / reports 是只读查询路径，无研究触发端点。
- [ADR-0049：Continuous Research Loop](0049-continuous-research-loop.md)：研究侧组合根、budget、durable state、anchor 与 fail-stop 语义。
- [ADR-0070：P7 partial experiment fail-stop](0070-p7-partial-experiment-fail-stop.md)：失败轮不得自动重放。
- `research/loop/compose.py`：`SyntheticLoopConfig`、`LoopWiring`、`open_synthetic_loop`、`run_unattended_and_report` 与 `settings_fingerprint`。
- `apps/worker/loop.py`：`LoopBudget` 和有限轮次 `ResearchLoop.run_unattended`。
- `research/loop/durable.py`：durable header、cross-check、单写者锁和 `FileAnchor`。
- `research/reports/loop.py`、`apps/api/app.py`：round-report writer 与只读 API report root。
