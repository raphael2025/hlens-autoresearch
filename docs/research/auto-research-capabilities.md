# 自动研究能力：现状与规格缺口

| 字段 | 值 |
|---|---|
| 性质 | 研究规格索引（人类可读）：记录自动研究能力**今天实现到哪里**、依据哪条已接受的 roadmap / ADR、以及哪些能力尚无批准规格 |
| 基线 | `main` @ `44fe9a2`（2026-09-27 模块审计基线）；逐项证据来自代码、roadmap 与 ADR 的只读核对；本文为待验收状态索引 |
| 权威性 | 本文件**不批准**任何设计。标为"验收标准（提案）"的内容只是建议，须经 ADR / Codex / Raphael 决定后才生效；与已接受 ADR、Constitution、roadmap 冲突时以后者为准 |
| 相关 | [feature-library.md](feature-library.md) · [state-library.md](state-library.md) · [event-library.md](event-library.md) · [outcome-library.md](outcome-library.md) · [factor-library.md](factor-library.md) · [strategy-library.md](strategy-library.md) · [risk-library.md](risk-library.md) · [roadmap.md](roadmap.md) · [constitution.md](constitution.md) |

## 状态词汇

| 值 | 含义 |
|---|---|
| `DESIGNED` | 已接受的 ADR / roadmap 定义了它，但没有代码 |
| `IMPLEMENTED` | 有代码与定向测试 |
| `DEBUG_PENDING` | PROJECT_STATUS / ADR 标为 FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING：未经独立调试与复核 |
| `ACCEPTED` | 有 Codex / Raphael 的正式接受记录（`docs/reviews/`、ADR 状态为 Accepted）。本文件中"ADR `ACCEPTED`"只指设计被接受，实现状态另列 |
| `UNSPECIFIED` | 没有已接受的 ADR / roadmap 条目定义它 |
| `REQUIRES_DECISION` | 需要新 ADR、契约变更、Profile 数值或对已接受规则的解释才能继续 |

一项能力可同时为 `IMPLEMENTED · DEBUG_PENDING`。本文件中**没有**任何能力的 Phase 被验收：Phase 1 未验收；Phase 0.5、2～14 为框架 / 代码完成、待调试。

## 总体判断

系统今天可以**登记、计数、运行并筛选已声明的候选**：每个候选都是目录中某个已实现策略在其声明参数空间中的一个点，
经知识条目、人工审阅过的 LLM 草稿、批量网格或进化算子产生，预先登记进试验账本，运行"策略 → 风控 → 回测 → G0–G4"，
被拒绝或失败的写入 Failure Registry。系统今天**不能**自动产生新的 Feature / State / Event 定义，**没有**任何训练型模型或
超参数优化器，也不能在不写代码的情况下产生新的策略规则。Profile 数值未冻结，因此没有任何筛选结果是有效判定。

## A. 候选生成、筛选与过拟合控制

| 能力 | 依据 | 实现 | 测试 | 状态 |
|---|---|---|---|---|
| 预登记与试验账本（每次登记 / 重评 = 一次试验，失败也计数） | Constitution A1、C-T1；roadmap Phase 7 验收；ADR-0040 §3 | `research/hypotheses/ledger.py`；循环把 family 试验数传给 G3 | `tests/research/hypotheses/test_hypotheses.py`、`test_durable_ledger.py`、`tests/research/loop/test_loop_e2e.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 组合算子 DSL（产出假设，不产出代码） | 04-research-loop §4；ADR-0040 §2 | `research/hypotheses/dsl.py`（6 个构造器只生成带文字与引用的 Hypothesis；没有可执行 typed plan） | `test_hypotheses.py` | `IMPLEMENTED · DEBUG_PENDING`（规格生成，不代表执行） |
| P6 条件化评估（状态 × 策略单元） | roadmap Phase 6；ADR-0039 | `research/experiments/state_strategy.py`、`research/loop/trials.py`；矩阵报告接线在本地协调分支 | `tests/research/experiments/`、`tests/research/loop/test_loop_conditional.py` | 计算逻辑 `IMPLEMENTED · DEBUG_PENDING`；完整报告链待验 |
| P7 六类 DSL 算子进入批次运行 | ADR-0040 §2；现行批次明确 fail closed | `HypothesisBatch` 仅允许 `parameter_point`；六类 DSL 均在 `NOT_RUNNABLE_KINDS`。Conditioning 的 P6 单元评估是另一条执行通道 | — | `REQUIRES_DECISION`：typed AST、ref 类型检查、语义版本、身份绑定、因果边界与 trial 计数需先定义 |
| 批量网格（算子 × 策略 × 声明点；算子须人工审阅） | ADR-0040 §5 | `research/hypotheses/batch.py` | `tests/research/hypotheses/test_batch.py`、`tests/research/loop/test_loop_batch.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 知识 / LLM 假设草稿（严格结构，人工审阅后登记，LLM 不判定） | ADR-0040 §1、§4 | `research/hypotheses/generator.py`；只有脚本化 LLM | `test_strict_llm_drafts.py`、`test_knowledge_source.py`、`test_loop_llm_rejection.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 真实（联网）LLM Provider | ADR-0040 §1 只声明属后续批次，未设计 | — | — | `UNSPECIFIED`（需网络与凭据授权） |
| 自动产生 Feature / State / Event 定义（如窗口枚举、特征组合） | 无 | — | — | `UNSPECIFIED` |
| 事件交互 DSL（人工写表达式，编译为规格） | ADR-0061 | `plugins/events/dsl.py` | `tests/plugins/events/test_dsl.py` | `IMPLEMENTED · DEBUG_PENDING`（编译出的规格不是账本试验） |
| 研究假设 / 实验规格的执行隔离 | roadmap Phase 7 验收；09-security.md §4 | 当前 LLM 只产出严格 Schema 的数据草稿；Research Agent 不执行任意代码，loop 只调用固定 Provider | — | `REQUIRES_DECISION`：roadmap 的“沙箱生效”须定义为固定入口、人工审阅与资源隔离，不得授权 Research Agent 任意代码执行 |
| 进化算子（`mutate` 限声明空间、`combine`） | roadmap Phase 12；ADR-0045 | `research/evolution/` | `tests/research/evolution/` | `IMPLEMENTED · DEBUG_PENDING` |
| G0–G3、G5、负对照（打乱 / 平移 / 盲标签，可选多种子） | roadmap Phase 4；ADR-0037 §3 | `research/validation/pipeline.py`、`controls.py`、`stats.py`（Bonferroni / Šidák） | `tests/research/validation/test_pipeline.py` 等 | `IMPLEMENTED · DEBUG_PENDING` |
| G4 稳健性、PBO（CSCV）、DSR、回溯审计 | roadmap Phase 8；ADR-0041 | `robustness.py`、`overfitting.py`、`g4.py`、`retro_audit.py` | `test_robustness.py`、`test_g4_*`、`test_retro_audit.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 数据切分：purge / embargo、walk-forward、purged k-fold、封存样本外（每 family 一次） | Constitution C-L5、C-S1..4；ADR-0037 §3 | `splits.py`、`sealed_oos.py` | `test_splits_and_sealed_oos.py`、`test_durable_sealed_oos.py` | `IMPLEMENTED · DEBUG_PENDING` |
| C-L5 embargo 覆盖 horizon 的检查点 | Constitution C-L5；D-30 开放 | `G1.embargo_covers_horizon` 只比较绑定的单个标签规格 | FAIL 分支测试：本次新增（见下） | `IMPLEMENTED`（单规格）· 跨对象检查点 `REQUIRES_DECISION`（D-30） |
| 阈值来源 | Constitution C-A1、C-A7/A8；ADR-0007 | 所有判定阈值经 `threshold(profile, path)` 读取，未硬编码 | `test_every_threshold_is_read_from_the_profile` | `IMPLEMENTED · DEBUG_PENDING`；Profile 数值 `REQUIRES_DECISION`（D-09） |
| 空模型 / 门校准证据 | ADR-0037 §6；ADR-0042 | `research/validation/calibration.py`、`research/synthetic_lab/` | `tests/research/synthetic_lab/` | `IMPLEMENTED · DEBUG_PENDING`（只给证据，不选数值） |
| 训练型状态模型的固定窗口与种子 | roadmap Phase 2；ADR-0035 | 执行器强制窗口截断与必填 seed；两个分位模型为确定性，seed 不影响输出 | `tests/infrastructure/state/test_state_runner.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 真正的训练型模型（聚类、HMM、回归、分类器） | 无（state-library 只列为可能的方法） | 无 | — | `UNSPECIFIED` |
| 超参数搜索器（超出声明点的穷举） | 无 | 只有声明点的穷举（G4 网格、批量、变异） | — | `UNSPECIFIED` |
| 研究可拟合接口（逐折在清除后的训练标签上拟合） | ADR-0041 §6 第 4 项 | `controls.py::FittableStudy`，没有生产实现 | 管线测试 | `IMPLEMENTED · DEBUG_PENDING`（无使用方） |
| 持续研究循环（摄取 → 状态 → 假设 → [进化] → 实验 → 验证 → 记忆） | roadmap Phase 11；ADR-0044 / 0049 / 0050 | `apps/worker/loop.py`、`research/loop/` | `tests/research/loop/` | `IMPLEMENTED · DEBUG_PENDING` |

## B. 策略与风控候选、验证晋升与人工审查边界

| 能力 | 依据 | 实现 | 测试 | 状态 |
|---|---|---|---|---|
| 策略 / 风控 / 回测 Provider 契约，3 个研究策略，1 个风控政策 | roadmap Phase 5；ADR-0038 | `research/strategies/`、`plugins/backtest/` | `tests/research/strategies/`、`tests/contract_suites/{strategy,risk,backtest}.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 策略参数空间强制（空间外的点被拒绝） | ADR-0038 §3 | `research/strategies/_params.py::resolve_params` | `test_strategy_library.py` | `IMPLEMENTED · DEBUG_PENDING` |
| 声明空间的每个点自动计入账本 | 无（今天只有显式登记的假设计入；G4 网格重跑不计入） | — | — | `REQUIRES_DECISION`（C-T1 的解释） |
| 不写代码产生新策略规则 | 无；roadmap Phase 7 禁止执行未审查的生成代码 | — | — | `UNSPECIFIED` |
| 风控参数空间：声明 | ADR-0038 §3（"在模块中声明"） | `VOL_TARGET_PARAM_SPACE` | 无 | `IMPLEMENTED · DEBUG_PENDING` |
| 风控参数空间：强制、计数、候选搜索 | ADR-0038 未要求 | 无 | — | `REQUIRES_DECISION` |
| 循环 / 数据集路径向风控提供波动率信号 | ADR-0049 只写"策略 → 风控 → 回测" | 无（`risk_signals` 只在测试中填充） | — | `REQUIRES_DECISION` |
| 回测执行模型（参与率、冲击、空头借券 / 现金借款费率）与跨 bar 结转 | ADR-0038 实施说明；ADR-0054（Raphael 批准） | `plugins/backtest/execution.py`、`bar.py` | `tests/plugins/backtest/` | ADR-0054 `ACCEPTED`；实现 `IMPLEMENTED · DEBUG_PENDING` |
| G4 容量的成交量来源与未成交余量 | ADR-0064、ADR-0065（Codex Accepted） | `research/validation/`、`research/strategies/validation.py` | `test_backtest_validation.py` | ADR `ACCEPTED`；实现 `DEBUG_PENDING` |
| 生命周期中必须具名人工批准的转移（OOS → PAPER 等） | ADR-0006 | `core/lifecycle/strategy.py` | `tests/test_lifecycle*.py` | 人工批准转移 `ACCEPTED`（Phase 0 关闭复审）；此后新增的 VALIDATION → FAILED（ADR-0053）为 `DEBUG_PENDING` |
| 循环自动推进的上限（最多到 OOS） | ADR-0049；P12-LOOP | `apps/worker/loop.py`、`research/loop/stages.py` | `tests/research/loop/` | `IMPLEMENTED · DEBUG_PENDING` |
| 封存样本外的开封（每 family 具名批准） | Constitution C-S1..3 | `research/loop/trials.py`、`sealed_oos.py` | 见上 | `IMPLEMENTED · DEBUG_PENDING` |
| Profile 冻结登记（Promotion 的权威冻结来源） | ADR-0062 | `infrastructure/registry/profile_freeze.py` | `tests/promotion/test_profile_freeze_registry.py` | ADR `ACCEPTED`；实现 `DEBUG_PENDING`；登记为空 |
| Promotion 链（Registry、Promotion 服务、Equivalence Gate） | ADR-0005 | `research/promotion/`、`apps/promotion/` | `tests/promotion/` | `IMPLEMENTED · DEBUG_PENDING`（今天拒绝所有策略） |
| 替换提案（恒待人工批准，循环外的显式作业） | ADR-0045；P12-LOOP | `research/evolution/proposals.py`、`replacement_job.py` | `tests/research/evolution/test_replacement_*.py` | `IMPLEMENTED · DEBUG_PENDING`；循环内触发有意暂缓 |
| 纸面路由 | ADR-0043 | `research/router/` | `tests/research/router/` | `IMPLEMENTED · DEBUG_PENDING` |

## 规格缺口与验收标准（提案，未批准）

以下每项都没有已接受的规格。"验收标准（提案）"列出若将来批准该能力，至少应当由测试证明的条件；它们来自现有
Constitution 原则与已实现能力的同类要求，不设任何数值阈值，不代表方向已被选定。

| ID | 能力 | 为何需要决定 | 验收标准（提案） | 状态 |
|---|---|---|---|---|
| AR-1 | 自动产生 Feature 候选（对已实现 Provider 的声明参数做枚举，如窗口 `n`） | Feature 契约没有参数空间字段；生成的 FeatureSpec 是否、如何计入 family 试验数（C-T1）未定义 | 枚举是声明式的（Provider × 预先声明的取值集合），不生成代码；每个生成的规格先登记、再计算；同一输入永远生成同一组规格与哈希；每个生成规格通过 Feature 契约套件（含因果扰动）；试验计数包含全部生成候选（含失败） | `REQUIRES_DECISION` |
| AR-2 | 交互 / 变换 / 集成 / 否定算子的执行 | DSL 只定义为假设；执行语义（产生新信号还是新策略）与计数方式未定义 | 算子只组合已登记规格；输出可追溯到每个输入的规格哈希；组合不引入新的可见性（输出的可用时间 = 输入中最晚者）；每个组合计入试验数；否定算子作为对照而非新候选 | `REQUIRES_DECISION` |
| AR-3 | 训练型模型（状态模型或预测模型） | 重估节奏、拟合结果的持久化与哈希、HMM 状态编号对齐、计算成本（D-STATE-INC 已暂缓增量路径）均未定义 | 只见固定尾随窗口内的可见输入；随机性只来自规格 `seed`，同种子逐位复现；Outcome 不作为输入（监督模型的训练标签以 ADR-0041 §6 第 4 项的 `FittableStudy` 为先例：只在清除后的训练折标签上拟合；扩展到新模型族须单独批准）；契约套件因果扰动通过；不做全样本拟合 | `REQUIRES_DECISION` |
| AR-4 | 超参数搜索器（自适应搜索，而非穷举） | 自适应搜索的每个被评估点都是试验；目前只有穷举声明点 | 搜索空间预先声明；每个被评估点写入账本（含失败）；搜索只用研究窗口，从不接触封存样本外；给定种子可复现；过拟合检查使用完整试验数 | `UNSPECIFIED` |
| AR-5 | 风控候选搜索与风控参数计数 | ADR-0038 只要求声明 | 见 risk-library 缺口 R-1 | `REQUIRES_DECISION` |
| AR-6 | 循环中的风控信号 | ADR-0049 未定义来源 | 风控信号经 Feature 执行器从同一 PIT 数据计算并绑定其结果哈希；缺失时仍 fail closed（空仓）并在报告中可见 | `REQUIRES_DECISION` |
| AR-7 | 声明网格全部计入账本 | C-T1 的"假设族累计值"是否包括未显式登记的声明点 | 由决定给出；两种解释都须在 G3 / G4 中使用同一试验数 | `REQUIRES_DECISION` |
| AR-8 | C-L5 跨对象检查点（D-30） | 已登记为开放决定（Phase 4 前） | 由决定给出 | `REQUIRES_DECISION` |

## 本次随规格一并完成的实现事项

| 事项 | 依据 | 状态 |
|---|---|---|
| 研究侧信号复算与 Feature Provider 逐值相等（`research/strategies/signals.py`） | ADR-0030 / ADR-0038（研究辅助函数，不属 Phase 1 验收项） | CODE_COMPLETE · REVIEW_PENDING（`1023afb`） |
| `G1.embargo_covers_horizon` 的 FAIL 分支测试（不改代码） | Constitution C-L5；roadmap Phase 4"purging / embargo 实现并测试" | 只加测试 · REVIEW_PENDING（`7b9df59`）；不代表 D-30 已决定 |
| 两处过时的限制说明（`plugins/llm/README.md` 的 LLM 内容核对、`research/states/README.md` 的诊断写入器） | ADR-0040、ADR-0035 | 只改文档 · REVIEW_PENDING（`448c352`） |
