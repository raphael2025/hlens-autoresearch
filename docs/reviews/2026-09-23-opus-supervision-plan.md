# Phase 0 修复与最小研究闭环：Opus 执行方案

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-23 |
| 文档性质 | 任务方案与审查交接；不是新的项目状态文件、宪法或路线图 |
| 当前 Phase | Phase 0，仍未关闭 |
| 审查基线 | `2e2a0ad1ef2a0194348d135bcd748e7591545624` |
| 工作分支 | `phase/0`，从上述 main 基线创建 |
| 授权来源 | Raphael 要求：由 Codex 控制 WSL 中 Claude Code，使用 Opus 按审查建议继续；Codex 只负责控制与文档，不做编码任务 |
| 本轮授权 | 只读复核、执行方案、Proposed ADR 与项目状态文档同步 |
| 未获批准 | 具体冻结契约修改方案、Constitution 批准、Phase 关闭/开启、main 合并、环境安装 |

## 1. 分工与控制方式

- Raphael：批准架构选择、冻结契约变化、宪法、Phase 开启/关闭及 main 合并。
- Codex：下达有边界的任务、检查 Claude 输出与 Git diff、复核测试证据、维护交付文档；不编辑实现、测试、Schema 或依赖文件，不代替 Claude 修代码。
- Claude Code / Opus：分析实现；获批后编写代码、回归测试、重新导出 Schema、运行检查并提交可恢复的改动。
- 同一时刻只运行一个写仓库的执行会话，core 修改串行进行。
- 使用本机 Claude Code 的非交互 CLI；显式 `--model opus`，从输出元数据核验实际模型。不得静默改为 Sonnet、Haiku 或 `opusplan`。
- 首轮只开放 Read、Glob、Grep，不开放 Bash、Edit、Write 或子 Agent。后续实现轮次在获批任务范围内另行开放必要工具；不使用跳过全部权限检查的选项。
- 本轮不修改用户全局 Claude 配置。专用会话显式读取 CLAUDE.md、PROJECT_STATUS.md、PROJECT_MEMORY.md、AGENTS.md。
- 只读复核不能声称运行过新的反例或测试；先前审查结果与本轮执行结果必须区分。

## 2. 原审查证据与待复核发现

以下为 Codex 的独立审查结论，提供给 Opus 复核，不是必须服从的技术结论。允许根据仓库证据驳回、缩小范围或调整优先级。

原审查验证：Python 3.13.15；124 passed, 2 deselected；ruff check 通过；format 为 80 files already formatted；mypy 为 Success: no issues found in 21 source files。两个导出临时文件的测试未执行，35 份 Schema 在内存中与模型逐一比较一致，139 个本地引用可解析。不能把这次结果表述为完整 126 项通过。

| ID | 原严重性 | 发现与证据 | 本轮处理 |
|---|---|---|---|
| R01 | P0 | Contract frozen=True 不能阻止 params、dependencies、degradation_thresholds 等嵌套字典修改；已在内存验证内容哈希改变 | 起草独立 ADR：不可变载荷与版本绑定 |
| R02 | P0 | experiment_hash 仅哈希 repro；ExperimentSpec.strategy/risk_policy/outcome 在外部，不同引用可产生相同实验哈希 | 起草独立 ADR：实验/运行/结果身份 |
| R03 | P1 | Feature/State 可接受 Outcome，Event lag 可为负，具体 Spec 的 kind 可被覆盖 | 复核局部修复与契约变更边界 |
| R04 | P1 | INCONCLUSIVE gate 可整体 PASS；仅 G0 可整体 PASS；NaN gate 可通过但 JSON 往返失败 | 区分当前 DTO 不变量与未来完整验证门服务 |
| R05 | P1 | Profile 选择可为空、SelectionEntry 引用类型与版本可冲突；Run/Report/Artifact 缺乏完整可核验关联与结果清单；LLM 只有哈希 | 列出必要契约变化与可延期服务实现 |
| R06 | P1 | REVALIDATION→RETIRED 不要求审批；LifecycleHistory 构造不检查 transition.subject 一致性 | 核对 ADR-0006，提出最小回归修复 |
| R07 | P1 | LIVE 接受过期授权；受信执行策略边界未定义 | 区分本地时间不变量与未来授权服务 |
| R08 | P1 | event_time+declared_latency 不足以表达迟到/修订数据可用性 | Phase 1 前的时间语义决策；不要擅自选择方案 |
| R09 | P1 | 未知 schema_version 被接受；Python 校验不在 JSON Schema；哈希/序列化跨语言约定不全；Profile status 改变内容哈希 | 列入 ADR 影响面，避免强行实现未来服务 |
| R10 | P2 | Profile 时长等结构约束不足；部分方法字段与 lifecycle 归属过早冻结 | 校验结构，不选择最终阈值，不代决 Q-6 |
| R11 | P2 | trial/OOS 为自报计数，无权威账本关联 | 定义后续审计责任，不提前做数据库 |
| R12 | P2 | 测试偏配置检查，漏嵌套修改/身份覆盖/跨对象错配；导入测试不能形成安全隔离 | 给出有行为意义的回归测试矩阵 |
| R13 | P2 | 文档承诺 Phase 0 Provider 签名，代码缺失 | 选择实施或正式延期的决策包，避免十套无消费者接口 |
| R14 | P2 | README、Memory、roadmap、Status 环境/Phase 描述不一致 | 文档事实同步；不改历史 ADR 正文 |
| R15 | P2 | D-04 验证时序未决；worker 调用研究而 apps 禁止导入的边界待明确 | 提出后续路线图选项，不提前开启新 Phase |
| R16 | P3 | 后续逐行 Pydantic、序列化、历史复制和并发可能有成本 | 只列测量计划，不提前优化 |

## 3. 分轮交付

| 轮次 | Claude 工作 | Codex 验收 | 进入条件 |
|---|---|---|---|
| A：方案与决策 | 只读复核 R01/R02/R06，起草两个独立 ADR、修复批次、必要决策包 | 对照源码和原 ADR；剔除过度设计；形成可批准文件 | 本轮已授权 |
| B：Phase 0 修复 | 按已批准 ADR 串行修复，先写能暴露原缺陷的测试，再实现 | 查看 diff、原失败/修后通过证据、完整检查、范围与文档同步 | Raphael 批准具体 ADR 与修复批次 |
| C：关闭复审 | 逐条验收矩阵，列出未解决问题、迁移/旧版本读取影响 | 独立复核；通过不代表自动关闭 | B 完成，检查真实通过 |
| D：最小闭环提案 | 提出一条小规模可复现研究路径，明确模块、数据、对照和成功/停止条件 | 评估是否改善研究判断，限制运维成本 | 只做提案；实现需正式路线图/Phase 授权 |

代码修复建议分批：① R01；② R02 及必要证据绑定；③ 已接受不变量修复 R03/R04/R06/R07；④ 版本/Schema/文档一致性。实际边界以获批 ADR 和复核结果为准。不得用“修复”名义静默修改规则或扩大 Phase。

## 4. 每批必须交付的证据

1. 基线 commit、实际 Opus 模型、任务范围、涉及契约与 ADR。
2. 改动文件列表、关键行为前后差异、迁移影响。
3. 回归测试为何独立于实现；原实现是否实际失败，不得伪造 red 结果。
4. 实际运行的 pytest、ruff check、ruff format --check、mypy 原始结果和退出码。
5. Schema 重新导出与语义检查；通过生成一致性不等于领域不变量完备。
6. 工作区/分支/commit、未解决问题、是否需要人类决定。

Codex 可读取源码、diff 和现有检查输出，也可独立运行现有验证命令；不新增或修订测试代码。发现代码问题后把证据返回给 Opus，由 Opus 修复。

## 5. 最小研究闭环的后续提案方向（未批准）

目的：先证明系统改善研究判断，再决定是否扩展完整蓝图。

- 固定输入快照、明确版本的规格、一次确定性运行、绑定证据的验证报告、保留成功与失败记录。
- 同一输入重跑可复现；未来数据/切分错误/成本遗漏的负面样例能够被明确识别。
- 使用噪声对照和植入效应评估判别能力，方法与验收规则必须在观察结果前确定；本轮不决定最终统计阈值。
- 明确 OOS 暴露与校准集分离；不因为演示而使用未获授权的真实或旧数据。
- 将早期正确性样例与后期大规模 SyntheticMarketProvider 区分，是否调整现有路线图由 Raphael 决定。
- 暂不引入 LLM 自动发现、Router、自动演化、实盘和额外分布式服务。
- 若新增流程只增加字段和维护成本，却不提升可复现性、错误识别或研究效率，应缩减设计。

## 6. 决策边界

CLAUDE.md §5 要求冻结契约等重大变化“先写 Proposed 状态 ADR，Raphael 批准后才实施”。因此本轮把具体方案做成可审阅文件，停在该边界。不得把“让 Opus 继续”扩大解释为批准尚未展示的架构方案。

Q-7、H-3～H-7、最终阈值、Constitution 批准、Phase 开启/关闭和 main 合并均保持未决。

## 7. CLI 依据

已在本机确认 Claude Code 2.1.278 支持 `-p`、`--model`、`--tools`、`--permission-mode`、`--safe-mode` 和 JSON 流式输出。

- [Claude Code CLI](https://code.claude.com/docs/en/cli-reference)
- [模型配置：opus 与 opusplan 的区别、实际 modelUsage](https://code.claude.com/docs/en/model-config)
- [非交互运行](https://code.claude.com/docs/en/headless)

模型 alias 会随版本/服务商变化。实际模型以本次调用结果为准；不得仅因指定了 opus 就宣称已验证实际执行模型。

## 8. 本轮执行记录

### 首轮与协调者复核

- 本机 Claude Code：2.1.278；登录状态已确认，未修改登录或配置。
- 专用会话：`e359a62b-43f3-4b8a-9ffd-eb14e6d32266`。
- 启动请求：`--model opus`；初始化事件、assistant 消息和最终 `modelUsage` 均为 `claude-opus-5`，没有其他模型用量。
- 工具：仅 Read、Glob、Grep；plan 权限；首轮成功退出。Claude 未获得代码执行或写文件工具。
- 首稿有 375 行。Codex 未直接采纳，而是退回修订：审批图的触发条件不等于免审批、LLM 哈希记录不能替代可取回输入输出、不能全局排除 ID/审计时间、语义变化需 major、不能把未检查的外部数据断言为不存在。
- R02 精确定位：实现遵循“哈希复现元组”的既有定义；不足在于冻结契约的元组没有覆盖策略/风控/Outcome，因此必须 ADR，而不是悄悄修实现。
- 协调者要求缩小推荐方案：不引入 seed 派生 DSL、不冻结 run_id 生成算法，不提前实现 Registry/Runner。
- 修订继续使用固定模型 `claude-opus-5`，同样只读。具体选择仍为 Proposed，不构成批准。
- 原始会话事件和未采纳首稿保留于本机 `/home/raphael/.codex/hlens-supervision/2026-09-23/`，不作为正式决策来源，不提交会话原始日志。

### 最终交付与验收

- [ADR-0008：只读载荷与哈希载荷](../adr/0008-contract-payload-immutability.md)，Proposed。
- [ADR-0009：实验身份与内容绑定](../adr/0009-experiment-identity-binding.md)，Proposed。
- [Opus 复核与执行交接](2026-09-23-opus-phase0-handoff.md)：记录首稿纠偏、具体批次、D-11/D-12 批准选项与后续最小闭环方向。
- 两份 ADR 均获批、并明确授权 B1/B2 后才开始串行编码；版本变更尚未发生，2.0.0 只是提案目标。
- 两轮会话结果均为 success，实际模型只有 `claude-opus-5`，实际工具仅 Read/Glob/Grep，permission_denials 均为空。
- 协调者实际运行文档检查：`PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 UV_OFFLINE=1 uv run --no-sync --offline pytest -p no:cacheprovider tests/test_docs_consistency.py`，退出码 0，原样结果：`7 passed in 0.04s`。
- `git diff --check` 通过；源码、测试、JSON Schema、依赖文件无 diff。全量 pytest、ruff 和 mypy 本轮未重新运行，不以历史结果冒充本轮结果。
- 当前 Phase 仍为 0；Constitution 仍为 Draft；既有 Accepted ADR 正文未修改；未合并 main；没有远程，PR 为 none、CI 为 not configured。

本轮改动文件（全部 Markdown）：

1. `PROJECT_STATUS.md`
2. `PROJECT_MEMORY.md`
3. `README.md`
4. `core/README.md`
5. `core/domain/README.md`
6. `core/contracts/README.md`
7. `core/lifecycle/README.md`
8. `core/errors/README.md`
9. `docs/research/roadmap.md`（仅当前 Phase 状态行）
10. `docs/adr/README.md`
11. `docs/adr/0008-contract-payload-immutability.md`
12. `docs/adr/0009-experiment-identity-binding.md`
13. `docs/reviews/2026-09-23-opus-supervision-plan.md`
14. `docs/reviews/2026-09-23-opus-phase0-handoff.md`

未解决事项：D-11/D-12 具体方案批准、后续 B3 任务授权与规则覆盖、Provider 签名交付范围，以及原有 Q-7/H-3～H-7 等开放项。`ARCHITECTURE_DECISION_REQUIRED = YES`。
