# ADR-0068：Phase 7 类型化组合算子计划与执行边界

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | **Codex 依 Raphael 2026-09-27 项目全权委托接受** |
| 起草者 | Codex 子代理 |
| 相关 Phase | Phase 7（依赖 P5 / P6、P0.5） |
| 影响范围 | Research / Plugin / Experiment Runner；不修改冻结契约、Schema、Constitution 或 `core/` |
| 是否破坏兼容 | 否；现有声明、假设生成、`parameter_point` batch 和实验复现元组保持不变 |

## 背景（Context）

Phase 7 路线图列出 conditioning、interaction、temporal、transformation、ensemble、negation 六类组合。ADR-0040 §2 要求组合器只产出规格，不执行生成代码；`docs/architecture/04-research-loop.md` §4 要求每次组合计入 trial count。

当前 `research/hypotheses/dsl.py` 的六个函数把 `Ref` 拼入自然语言 `Hypothesis.statement` / `conditions`，并写 `origin_refs`；它们没有生成能被 Runner 消费的类型化计划。`research/hypotheses/batch.py` 的 `parameter_point` 才是当前可执行的批次种类；六种 DSL kind 明确列入 `NOT_RUNNABLE_KINDS`，构造批次时拒绝。`ExperimentSpec` 通过冻结的 `ReproducibilityTuple` 绑定 hypothesis、strategy、依赖哈希、代码提交、插件版本、数据快照及验证设置。不能把自然语言条件当执行指令，也不能只把计划哈希塞入无关文本字段而声称可复现。

现有材料对六类算子的业务语义描述不足以直接推出唯一执行行为。例如：Feature 对齐与缺失值规则未定义；`temporal` 的“bars”未绑定具体时间轴 / 日历；`standardize` / `quantile` / `smooth` 未声明统计范围与窗口；ensemble 的代码描述为等权投票但没有规定信号到仓位的映射；negation 没有定义仓位、风险限额、现金与成本下的逆向语义。Phase 6 条件化矩阵统计也不等于把策略仅在 State 条件成立时执行。

本提案冻结安全的执行架构和拒绝规则；不虚构以上语义，不改变六个既有 Hypothesis helper 的输出。

## 决策（Decision）

### 1. 执行模型与闭世界原则

1. 组合表达式是严格的数据 AST，不是 Python、SQL、自然语言或 LLM 可执行文本。解析器必须拒绝未知节点、算子、字段、类型、隐式转换、缺省参数、重复对象键、无界深度 / 节点数及未解析引用。界限必须由调用方显式给出，不设静默默认值。
2. AST 只有在**类型检查成功**，且每个节点都能由调用方显式提供、已登记、版本化并经人工审阅的 `OperatorImplementation` 精确 lowering 后，才可编译。未登记、实现版本或内容哈希不匹配、Provider descriptor 不匹配、引用哈希缺失、输出不能由现有版本化规格表达，一律在登记 trial 之前拒绝；不得降级成普通 `Hypothesis` 再让 Runner 猜执行行为。
3. 每个实现声明有限输入类型、输出类型、参数结构、语义版本、实现内容哈希、确定性声明、Provider / Plugin 身份、lowering 目标 kind、时间与缺失值规则（适用时）。运行器只接受 allowlist 中内容完全相等的实现；名称相同但 SemVer 或内容哈希不同不是同一实现。
4. Lowering 必须产生现有 Provider 可执行的版本化规格（FeatureSpec / StateSpec / EventSpec / StrategySpec 等），并引用已有依赖；实际实验仍由现有 `ExperimentSpec` → `ExperimentRun` 管线运行。生成代码、任意回调、运行时反射、动态导入和计划内的可执行字符串均不允许。
5. 编译出的规格必须进入现有 Registry / 依赖闭包解析，并以普通内容哈希被 `ReproducibilityTuple.dependency_hashes` 覆盖；策略及其传递依赖、Provider / Plugin 内容哈希、固定数据快照和代码提交仍按现有实验规则绑定。不得仅记录 AST 哈希而遗漏运行时真正消费的规格或实现。
6. 一次批次的所有派生 Experiment / Hypothesis 在任何运行前全量预登记；每个可执行派生实验恰计一个 trial（包括之后失败或拒绝的执行尝试），整批校验和登记原子化。重复内容按既有 `TrialLedger` 幂等规则处理。不得按实验结果选择性扩展或缩减计划。

### 2. 类型边界与六类算子登记表

以下是**名义输入 / 输出类型和目标规格 kind**，不是对未定义业务语义的授权。参数必须逐项显式声明，输出必须由对应已审阅 Provider 实现；在该实现登记且语义被明确以前，各算子仍 fail closed。

| DSL kind | 名义输入类型 | 目标输出类型 | 现有路线图 / 代码依据 | 本 ADR 的执行状态 |
|---|---|---|---|---|
| `conditioning` | `StrategyRef` + `StateRef` + 精确的 state label / 值 | 条件化 Strategy 计划，最终须 lower 为可执行 `StrategySpec` 或已定义的逐状态实验计划 | DSL helper 传入 strategy、state、字符串值；roadmap 例为“策略仅在某 State 下启用”；loop conditional 当前按 State 分组统计，不改变策略执行 | **拒绝执行**；策略门控、未知状态、逐单元 trial 与报告语义未统一 |
| `interaction` | `FeatureRef` × `FeatureRef` | 新 `FeatureSpec` | DSL helper 仅称 product / interaction；`FeatureSpec` 可声明输入，但未声明乘法、对齐及缺失值语义 | **拒绝执行**；运算 / 对齐 / 单位 / PIT 语义未定义 |
| `temporal` | `EventRef` + `EventRef` + 正整数窗口及其显式时间单位 | 新 `EventSpec` | helper 表达“第二事件在第一事件后 N bars 内”；ADR-0061 是独立的 Event DSL，使用已审阅 `seq` / `and` / `not` / `count` 与微秒窗口 | **拒绝执行**；bar 频率、端点边界、主体、可观察时刻及与 ADR-0061 映射未定义。本 ADR 不扩写或覆盖 ADR-0061 |
| `transformation` | `FeatureRef` + 已枚举变换名 + 显式参数 | 新 `FeatureSpec` | helper 名称白名单：`standardize`、`rank`、`quantile`、`difference`、`smooth` | **拒绝执行**；窗口、拟合范围、分位算法、状态 / 缺失值和可用时间语义未定义 |
| `ensemble` | `StrategyRef` 的非空、无重复有序 / 规范化集合 | 新 `StrategySpec` | architecture §4 写多信号投票 / 加权；当前 helper 接受任意 `Ref` 并在文案中称“equal-weight vote” | **拒绝执行**；输入 ref 类型、投票 / 权重、仓位、平局和风险约束语义不一致 |
| `negation` | `StrategyRef` | 新 `StrategySpec`，且必须声明其仅作为何种对照 | architecture §4 写反向策略对照；helper 写“inverse ... control” | **拒绝执行**；反向目标、现金 / 仓位 / 杠杆约束、成本与对照资格未定义 |

类型检查使用 Registry 中解析出的规格实体，而不是只信任 `Ref.kind` 文本；传递依赖必须通过现有 Registry / Runner 规则验证。任何对象 kind 不匹配、依赖未找到、版本不精确或输出不属于表中类型均拒绝。类型表不授权新契约字段，也不把类型相容当作行为语义完整。

### 3. 确定性、语义身份与审计

1. 实现清单身份为 `(operator_id, semantic_version, implementation_content_hash, provider_key, provider_content_hash)`。SemVer 只标识兼容系列；实现内容哈希才判定本次实现内容是否与审阅记录相同。
2. AST 使用仓库现有规范化内容哈希机制。原始 AST、编译器版本 / 内容哈希、显式限制、根输出类型、按拓扑顺序排列的节点、每节点输入引用及内容哈希、精确算子实现身份、lowered spec 引用 / 内容哈希必须进入可复核的计划记录。所有运行时可观察的排序均显式：不具备已声明交换律的算子按输入顺序；不得为哈希方便擅自排序。
3. 编译为纯确定性函数：相同 AST、相同已解析引用及哈希、相同 implementation allowlist、相同 compiler 内容版本，必须得到相同计划记录和相同 lowered spec 内容 / 哈希。时间、随机数、环境值和外部 I/O 不得隐式进入 lowering。若一个 Provider 声明的确定性条件不满足，则编译拒绝该执行模式。
4. 审计记录至少包含计划哈希、原始表达式哈希、编译器身份、每个实现及 Provider 身份 / 哈希、解析输入 refs / 哈希、输出 refs / 哈希、由此产生的 ExperimentSpec / `experiment_hash`、预登记 batch 与 TrialLedger 身份、拒绝码。记录追加保存；失败也保留，不回写旧记录。
5. 可复现的权威执行输入仍是普通 `ExperimentSpec.repro` 及其已绑定闭包。Plan record 是可重算的生成审计材料，须关联目标 ExperimentSpec 与哈希；不能替代 `ReproducibilityTuple`。如未来某实现无法将其全部执行语义映射到已绑定的版本化规格 / Plugin 内容，需另立契约 ADR 设计直接绑定方式，在该 ADR 获批前不得执行。

### 4. 与现有模块的边界

- **`ParameterPointBatch` / `parameter_point`**：当前唯一组合 batch 执行路径，保持现状。typed operator plan 不修改其格网、参数读取 / 回读、reviewed allowlist 或完整预登记行为。未来接入时复用全量预登记、不得改变 trial 计数规则。
- **Hypothesis / LLM**：六个旧 helper 仍只创建声明性 Hypothesis / `origin_refs`。LLM 输出是待审数据；人工审阅不自动赋予 DSL 执行许可。自然语言文本不会被解析成 AST。
- **Research Loop**：只有 compile + registry validation + preregistration 全部成功的 plan 才能交给 loop。任何拒绝都发生在首个 experiment 执行前；loop 不把一个无效组合记成 `ERRORED` trial。
- **Strategy / Feature / Event / State Providers**：操作实现由已有对应 Provider 负责，声明式算子层不内嵌计算实现。P3 Event DSL（ADR-0061）继续是事件表达式的独立契约；如复用它，必须由单独的已审阅 lowering 明确绑定其 DSL / 编译器版本与输出哈希。
- **跨 Phase 边界**：不引入由 P7 直接执行的新 Feature / State / Event 数据路径，不修改 P4 / P8 判定、Constitution、Lifecycle、Validation Profile 或生产 / 交易边界。新 Provider 能力依 Phase 范围与既有 Protocol 先行规则交付。

### 5. 首批可执行范围

本 ADR 只批准上述**机制和封闭边界**的 Proposed 方案，不代表批准实现。现有证据没有足够依据把六类中的任一项定为可执行语义。因此，初始安全子集明确为：

- 已有 `parameter_point`：保持可执行。
- `conditioning`、`interaction`、`temporal`、`transformation`、`ensemble`、`negation`：仅生成声明 / Hypothesis，全部不进入 typed plan execution，直到逐项语义规格与 Provider lowering 获批并实现。

每个操作首次设为 runnable 前须追加一份逐项决议（可为本 ADR amendment 或专用 ADR），至少冻结：完整参数 schema、输入 kind、输出 kind、时间 / PIT 规则、缺失值 / 对齐策略、可重复执行算法、输出规格编码、实现与 Provider 身份、旧实验兼容与验收证据。不得以 `CODE_COMPLETE` 或存在 helper 代替接受或 Phase 验收。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 把 `Hypothesis.conditions` / statement 直接交给 Runner | 改动最少 | 文字含义不唯一、没有类型与审计绑定；会让生成文本变成隐式代码 | 违反 ADR-0040、闭世界原则与可复现要求 |
| 立即把六类全部标记可执行 | 表面上覆盖路线图六类 | 当前仅有假设文字，多个输入类型与具体算法冲突 / 未定义，可能改变信息流与统计语义 | 不臆造行为；缺少证据不能安全定语义 |
| 在本次扩展 `core/domain/ExperimentSpec` / Schema 加计划字段 | 可直接绑定 plan hash | 改动冻结契约及 Schema，需要契约版本、迁移、生成与兼容审查；当前不必要，因为只有落成现有版本化规格并绑定闭包的算子才可执行 | 超出本 ADR 的安全最小边界；未来发现现有闭包无法绑定时另提契约 ADR |
| typed IR + 精确实现 allowlist + lower 到现有规格 | 可静态拒绝不合法计划；执行身份复用现有规格 / Repro 元组；按操作逐步扩展 | 前期必须逐项补齐语义和 Provider；未获明确实现的操作仍拒绝 | **本提案方案**；当前仅启用已存在的 `parameter_point` |

## 后果（Consequences）

- 正面：保留六类组合方向和现有兼容行为，同时把“声明”与“执行”分开；为逐算子语义审查和实现提供一致的安全门。
- 负面 / 代价：此 ADR 获批后仍不会使六类 DSL 可执行；六类各自需补充完整算法、Provider lowering 与验收工作。首批基础实现只解析并验证闭世界 AST，输出始终为 `non-runnable`；不预登记 Experiment、不增加 trial、不写执行审计。**任何 OperatorImplementation allowlist 在空集状态下不得编译 runnable plan。** 首个算子语义决议前，必须另行确定执行计划审计记录的持久化位置、与 TrialLedger 全量预登记的崩溃原子性及状态 / 指纹兼容；这些未解决前不得启用算子。
- 需要迁移的内容：无。既有 Hypothesis、TrialLedger、ParameterPointBatch、ExperimentSpec 和 Schema 不迁移、不重哈希。
- 对复现性的影响：现有 `parameter_point` 哈希与运行不变。未来可执行算子须绑定其生成规格的内容哈希和 Provider / Plugin 内容哈希；任何语义变化都需新版本 / 新哈希、新假设登记和新 trial，不能重放时切换至最新版。
- 安全边界：不执行 LLM 生成代码；不让 LLM 裁决；不通过组合算子修改验证门或 Profile。

## 合规检查

- [x] 本 Proposed 文档不修改 Domain Contract、Provider Contract、Schema、Constitution 或 `core/`。
- [x] 不改变 Validation、Lifecycle、Profile 数值或 Phase 顺序。
- [x] 不把自然语言 / LLM 内容作为执行代码。
- [x] 每次可执行组合均须预登记并计入 trial；失败路径不缩减 trial 数。
- [x] Research / Application Plane 边界不变；插件实现按现有 Provider 与 Phase 规则交付。
- [x] 清晰标明六类的执行语义未完整确定；初始运行子集不扩张。

## 接受记录（2026-09-27）

Codex 依 Raphael 对项目决策与开发的全权委托接受本 ADR 中的**闭世界类型化计划机制与拒绝边界**。此决定不接受任一算子的业务执行语义；六类仍全部 `NOT_RUNNABLE`，`parameter_point` 路径不变。实现只允许产生带显式资源限制的 `non-runnable` typed AST / validation result。OperatorImplementation allowlist 保持为空；不得把 typed result 交给 Research Loop、Provider 或 Runner。执行计划持久化及其与 TrialLedger 的原子预登记必须在首个算子启用前另行裁决并实现。在此之前，基础解析不得创建持久审计副作用或 runnable trial。

## 参考

- `docs/research/roadmap.md` §Phase 7（目标、输出、验收标准与禁止事项）
- `docs/architecture/04-research-loop.md` §§3–6（组合输出、六类方向、trial count、假设登记）
- `docs/adr/0040-hypothesis-generation-and-llm.md` §2（组合只产出规格；生成内容永不作为代码执行）
- `docs/adr/0061-interaction-dsl.md`（独立、封闭、类型化 Event DSL；不被本 ADR 改写）
- `research/hypotheses/dsl.py`（六类现有 Hypothesis helper）
- `research/hypotheses/batch.py`（`parameter_point` runnable；六类 DSL kinds fail closed）
- `core/domain/research.py`（冻结的 `ReproducibilityTuple` / `ExperimentSpec`）
- `core/domain/specs.py`（Feature / State / Event / Strategy 版本化规格及直接引用约束）
- `PROJECT_STATUS.md`（P7 当前状态：组合算子仍 fail closed）
