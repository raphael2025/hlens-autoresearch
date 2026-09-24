# Phase 0 关闭复审 C1（独立只读）

| 项目 | 内容 |
|---|---|
| 文档性质 | **历史审查记录**；不是状态文件、宪法、路线图或架构决定。结论只反映基线时的仓库状态，此后不回写 |
| 复审日期 | 2026-09-24 |
| 批次 | Phase 0 关闭复审（批次 C）第一轮 C1 —— Raphael 于 2026-09-24 明确授权 |
| 基线 | 分支 `phase/0`，HEAD `fce4f81efdf24e627d9ce37c46e3a80ea0c0ddf3`，工作树干净 |
| 复审者 | Claude Code（Opus 5.5），独立只读；未修改仓库、未安装依赖、未使用子 Agent |
| 固化 | 批次 C2a（docs-only）按 Codex 要求整理压缩后提交；证据与结论不变，仅修正了原报告中因多文件拼接输出造成的行号引用（见 §9） |
| 结论 | **`FIX_BEFORE_CLOSE`** |

> 复审通过或不通过都**不**构成 Constitution 1.0.0 批准、Phase 0 关闭、main 合并或 tag 的授权。
> Constitution 仍为 `0.2.0-draft`，属于 Raphael 的人工批准门。

## 1. 总体评估：CONDITIONAL

- 工程与契约基本达到可供 Raphael 决定的状态：四项工程检查全绿；38 份 current Schema 与仓库外
  新导出逐字节一致；B3 期间 v1 快照与既有 `tests/vectors/v1/` 零变化；R01–R16 大部分已关闭或按既定 Phase 正确延期。
- 仍有 1 个 P1 契约逻辑缺陷（F1），且按 D-25，2.0.0 一旦发布（合并 main / tag）后同类收紧必须升 major，
  因此发布前是修复成本最低的窗口。另有若干材料级文档漂移（F6）。
- Constitution 仍是 `0.2.0-draft`：**人类批准门，不是已通过项**。

## 2. 实际运行的检查（原样摘要）

| # | 命令 / 检查 | 结果 | 退出码 |
|---|---|---|---|
| 1 | `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync --offline pytest -p no:cacheprovider -q` | `1304 passed in 1.19s` | 0 |
| 2 | `uv run --no-sync --offline ruff check .` | `All checks passed!` | 0 |
| 3 | `uv run --no-sync --offline ruff format --check .` | `109 files already formatted` | 0 |
| 4 | `uv run --no-sync --offline mypy` | `Success: no issues found in 33 source files` | 0 |
| 5 | `git diff --check`；`git status --short` | 无输出；空 | 0 |
| 6a | `CONTRACT_SCHEMA_VERSION` / `len(CONTRACT_MODELS)` / `len(V1_MODEL_NAMES)` | `2.0.0` / 38 / 35（current 多出 `ContentBlobRef`、`GitCodeRevision`、`ProfileSelection`） | 0 |
| 6b | `export_json_schemas()` 导出到 `/tmp/c1schemas-ugxzs1n8`，与 `schemas/*.schema.json` 比较 | 38 对 38，名称集合相同，**0 份字节差异**；`schema_version` 默认值 current `{2.0.0}`、v1 `{1.0.0}` | 0 |
| 6c | 本地 `$ref` 解析（仅 `#/` 本地指针） | current 177 个 / 0 无法解析；v1 139 个 / 0 无法解析；无非本地引用 | 0 |
| 7 | `git diff cd84a4e..HEAD -- schemas/v1 tests/vectors/v1`（另用 `c81a548`、`2ff1798` 交叉核对） | 三者均为空；B3 各批提交 `2544d2a` `7f9892c` `04bb3f8` `d083273` `695a34b` `1ad9f59` `63c182d` `fce4f81` 在 v1 路径下改动文件数均为 0 | 0 |

补充证据：

- 基线选择理由：`cd84a4e` 是 B3 之前最后一个经 Codex 独立复验的恢复点。
- 全历史中 `schemas/v1` 只被 `4e0f6e3` 改动过，`tests/vectors/v1` 只被 `4f83e18` 改动过；
  35 份 v1 快照与 `066b22d:schemas/` 逐字节一致（35/35）。
- `tests/vectors/v1_coverage/` 是 `1ad9f59` 新增的独立目录（ADR-0016 验收矩阵第 15 项），未改动冻结向量。
- 环境：Python 3.13.15、pydantic 2.13.5；ruff / mypy 缓存重定向到 `/tmp`。
- ADR-0011 ~ 0017 自接受提交 `2ff1798` 后正文零改动。
- CI 未配置（远程仓库未定，ADR-0004）。

## 3. Phase 0 验收矩阵（`docs/research/roadmap.md` Phase 0 的输出、验收标准、禁止事项）

| 项 | 判定 | 证据 |
|---|---|---|
| 输出：批准的 Constitution v1.0.0 | **HUMAN_GATE** | `constitution.md:5-6` 仍为 `0.2.0-draft`、Draft |
| 验收：Constitution 不含数值阈值 | PASS | 人工全文复核；`tests/test_docs_consistency.py` 的数值检查 |
| 输出 / 验收：Profile、选择规则、Experiment Metadata 契约 | PASS（有 F1 缺陷） | `core/contracts/validation_profile.py:206-241`；`core/contracts/profile_selection.py:41-144` |
| 输出：`core/domain`、`core/contracts` 代码与 JSON Schema 导出 | PASS | `core/contracts/registry.py:66-113`；38 个模型 = 38 份 Schema，逐字节一致 |
| 验收：所有核心实体有契约与 Schema | PASS | `02-domain.md` §2 实体均覆盖；CostModel 不在实体表，Phase 4 交付（CORRECTLY_DEFERRED） |
| 输出 / 验收：状态机只允许定义的转移 | PASS | `core/lifecycle/strategy.py:63-107`；`tests/test_lifecycle.py` 独立写出期望表；探针确认 DEGRADED→ACTIVE、终态出边、OOS→ACTIVE 被拒 |
| 输出：错误分类 | PASS | `core/errors/__init__.py` |
| 输出：工程基线 | PASS | `.python-version` = 3.13、`requires-python ==3.13.*`、`uv.lock`、四项检查全绿 |
| 输出：ADR 0003+ | PASS | 0003 ~ 0017 均 Accepted |
| 验收：契约层无基础设施依赖 | PASS（P3 注记） | `tests/test_architecture_boundaries.py` |
| 验收：CI / 本地测试命令可运行 | 本地 PASS；CI CORRECTLY_DEFERRED | 远程仓库未定（ADR-0004） |
| 禁止：实现 Feature / Strategy / Backtest；接入真实数据；引入 LLM | PASS | `research/` `plugins/` `strategies/` `risk/` `apps/` `infrastructure/` `data/` 下除 README 外文件数为 0；`uv.lock` 无 LLM / 数据类依赖 |

## 4. 发现（F1 ~ F6）

### P0

无。

### P1

**F1 —— Profile 选择规则的"唯一映射"可被嵌套 `schema_version` 绕过。**
`ProfileSelectionRule._unique_keys`（`core/contracts/profile_selection.py:65-69`）用 `model_dump_json()`
比较 key，`select()`（`:71-81`）用 Pydantic `==`；两者都把每个嵌套 Contract 自带的
`schema_version`（`core/domain/base.py:302`）算进"键身份"。
探针：同一规则中两个语义相同的 key（`2.0.0` 与 `2.0.1`）分别映射到 `strict` / `lenient` Profile，
规则被**接受**；`select(key@2.0.0)` 返回 strict，`select(key@2.0.1)` 返回 lenient；
仅差 `+build` 的查询键找不到匹配。另外 `binance` / `Binance` 可作为两条不同条目共存。
违反 Constitution C-A4 与 ADR-0007 §3 规则 5（确定性映射、研究者不得自选）。
不是 P0：需要一份本身有缺陷的规则制品才能被利用，Phase 4 之前也没有消费方。

### P2

- **F2 —— v1 只读入口对非规范形式的 v1 载荷给出不同的旧身份。** `core/compat/v1.py:208-213`
  直接对原始载荷求哈希。探针：删掉 `lineage` / `applicable_instruments` 等默认字段后载荷仍被接受，
  但哈希 ≠ v1 模型当年会算出的值；嵌套非法内容也被接受（已如实声明为只做顶层检查）。
  文档（`v1.py:3-5`）写"按 v1 当时的语义"，未写明"输入必须是完整的 v1 持久化规范载荷"这一前提。
- **F3 —— 嵌套 `schema_version` 的语义未定义。** 探针：两个 `str()` 相同的 `Ref`，一个信封版本
  `2.0.1`，其 `==` 与 `content_hash` 都与 `2.0.0` 不同。生命周期 / 授权 / Risk Gate 的 subject 比较
  （`core/lifecycle/strategy.py:205,207,232,253`）与部署中的代码身份比较（`core/domain/artifact.py:121`）
  会因此误拒（fail closed）；内容哈希因信封版本不同而不同。
- **F4 —— 四项开放问题未登记在任何待决清单**：R08 迟到 / 修订数据语义（仅 ADR-0012 写了"不发明"）；
  R15 `apps/worker` 运行实验（`01-system.md:110`）与 apps 不得 import research（`:71`）的边界；
  C-L5 embargo ≥ 最长 Outcome horizon 的跨对象校验；C-L4 幸存者偏差 / 标的有效期。
- **F5 —— 生命周期转移接受空 `evidence`，且允许 `approved_by == triggered_by`。** 探针：
  VALIDATION→OOS 空证据被接受；OOS→PAPER 由同一 bot 触发并批准被接受。ADR-0006 §3 第 4 条把
  证据引用列为每条转移记录的组成部分。
- **F6 —— 材料级文档漂移**，见 §7。

### P3（摘要）

架构边界测试对 `apps/` `strategies/` `risk/` 空转且可被 `__import__` 绕过、无 `plugins → research`
规则；判定穷举测试以 `derive_verdict` 自身为基准（有显式用例缓解）；模型校验器中抛出的
`LifecycleViolation` 原样外抛而非 `ValidationError`；`ExperimentMetadata` 无 `run_id`；
`ProfileSelectionKey.research_class` 与 `ProfileScope.research_class` 约束不一致；
`Hypothesis(origin=llm)` 无法引用 `LlmCall`；CostModel 无契约；`FailureRecord.terminal_state` 是自由字符串。

## 5. R01 ~ R16 处置

| ID | 结论 | 核实依据 |
|---|---|---|
| R01 递归只读 / 别名隔离 / 哈希边界 | CLOSED | `base.py:186-264, 309-324`；`model_construct` 按 ADR-0010 声明为不受支持 |
| R02 实验身份 | CLOSED | `research.py:145-212`；传递闭包属 Runner（CORRECTLY_DEFERRED） |
| R03 Outcome 输入 / lag / kind | CLOSED | `specs.py:93-122`，具体规格 `kind` 为 Literal；只防声明层面 |
| R04 verdict / NaN / gate_id / 阈值来源 | CLOSED | `research.py:325-418`；`allow_inf_nan=False`（`base.py:298`）；门集合完整性延期 |
| R05 Profile 选择 / Run-Report / LLM | PARTIALLY_CLOSED | 结构与 kind 已关闭；**F1**；结果 manifest 与内容取回延期 |
| R06 退役审批 / 主体 | CLOSED | `strategy.py:98-107, 243-266` |
| R07 LIVE 授权 | CLOSED（结构）；执行点 CORRECTLY_DEFERRED | `strategy.py:193-215`；契约层有意不再拒绝 LIVE（ADR-0011 D-17.4），探针确认 |
| R08 迟到 / 修订数据 | CORRECTLY_DEFERRED（Phase 1） | ADR-0012；**未登记（F4）** |
| R09 未知 major / Schema 弱于运行时 / status 进哈希 | CLOSED | `base.py:326-342`；仅运行时约束已如实披露（07 §5.4）；另见 F3 |
| R10 Profile 结构约束 | CLOSED | `validation_profile.py:46-196`；Q-6 仍开放 |
| R11 trial / OOS 计数自报 | CORRECTLY_DEFERRED | `06-experiment.md` §7 |
| R12 测试质量 | PARTIALLY_CLOSED | 以行为测试为主、转移表独立写出；仍有空转测试与同源基准，缺 F1/F2/F5 反例 |
| R13 Provider 接口 | CLOSED（决策） | ADR-0017；**Provider Protocol = 0 是决定，不是遗漏** |
| R14 文档漂移 | PARTIALLY_CLOSED | 见 §7 |
| R15 D-04 / worker 边界 | CORRECTLY_DEFERRED（D-04 Phase 4） | worker 边界未登记（F4） |
| R16 性能 | CORRECTLY_DEFERRED | 见 §8 |

## 6. v1 / v2 迁移与旧版本读取

- current `2.0.0`：38 个模型、38 份 Schema；v1：35 份快照（与 `066b22d` 导出一致），只读入口
  `core/compat/v1.py:read_v1`，返回的 `LegacyV1Record` 不是 `Contract`。
- 探针：固定向量读取后哈希不变；`2.0.0` / `1.0` / `01.0.0` / `١.٠.٠` / 多余顶层字段 / 错模型 /
  仅 v2 存在的模型全部拒绝；v1 `ReproducibilityTuple` 载荷被 v2 模型拒绝（16 个校验错误）。
- v1 与 v2 的 `content_hash` / `experiment_hash` 不可比较，文档与代码一致。
- B3 期间 `schemas/v1/` 与既有 `tests/vectors/v1/` **无任何未授权变化**（§2 第 7 项）。
- 真实外部 v1 数据迁移：**NOT ENOUGH EVIDENCE**。F2 使非规范形式的旧载荷存在身份歧义。

## 7. 文档与实现漂移（F6）

| 文档怎么说 | 实际情况 |
|---|---|
| `README.md`："独立审查后的修复方案正在准备" | B1/B2/B3 已全部实施，批次 C 进行中 |
| `roadmap.md` 页首："修复方案待批准" | 同上 |
| `05-plugin.md` §7："位置划分存在歧义，见 D-03" | D-03 已由 ADR-0005 决定（§7 目录映射） |
| ADR-0011 ~ 0016 验收矩阵："实现尚未发生，矩阵未运行" | 已实施并复验；Accepted ADR 正文不可改（ADR-0001），ADR 索引已说明，保持现状 |
| `10-migration.md` §3："major 需要迁移脚本" | v1→v2 按 ADR-0008 / 0009 有意只提供只读读取器 + 重新登记 |
| `core/compat/v1.py:3-5`："按 v1 当时的语义"得到旧身份 | 仅当输入是完整的 v1 持久化规范载荷时成立（F2） |
| `07-validation.md` §2 "check_id"；`failure-registry.md` `hypothesis_family` | 实现为 `gate_id` / `hypothesis_family_id` |

## 8. 研究完整性、边界、演进与性能（摘要）

- **研究完整性**：原则层完整且未选择任何数值；契约层覆盖 look-ahead（lag 非负）、Outcome 不得作为输入、
  trial 计数字段、OOS 开封记录、Profile 引用 + 哈希三处绑定、失败 / 退役记录分离；
  运行时校验（泄漏门、权威账本、frozen 校验、成本模型内容）全部是诚实声明的延期义务。
  缺口：F1（研究者自由度漏洞）、F3，以及 F4 的四项未登记问题。
- **生命周期与研究 / 生产边界**：12 个状态、18 条边与 ADR-0006 一致；PAPER / ACTIVE + `execution_mode`
  语义正确；追溯链 Artifact → Equivalence → Deployment 结构已表达，生产拒绝加载、Registry 存在性属未来服务。
  契约层不再拒绝 LIVE：Phase 13 红线在 Control Plane 落地前只能由人与流程保证。
- **五年演进**：Backtest / State / Feature / LLM / 事件总线 / 前端 = EASY TO REPLACE；
  Python、存储、Rust/C++/远程服务 = MODERATE MIGRATION；Validation Profile 字段集在校准前冻结 =
  HIGH COUPLING RISK（Phase 4 预期一次 Profile major）。
- **性能**：NOW 无需优化；NEXT 1–2 PHASES 关注 `LifecycleHistory.append` 的 O(n²) 重校验与大 params 哈希；
  LATER 关注 Registry 规模哈希吞吐与跨语言规范化。

## 9. 关闭建议

**`FIX_BEFORE_CLOSE`**。范围窄：F1（P1）与 F6 文档修正；F3 / F5 宜在发布前由 Codex 裁决。
完成后剩余的是 Raphael 的人工门：Constitution 1.0.0、Phase 0 正式关闭、main 合并与 tag。

复审结束时 `git status --short` 为空，HEAD 仍为 `fce4f81`；仓库内唯一晚于 HEAD 提交时间的文件是
被 gitignore 忽略的 `.pytest_cache/v/cache/nodeids`（11:20:14），早于本复审的检查运行，非本复审写入。

**行号修正说明**：原始聊天报告中有几处行号取自多文件拼接的输出，固化时已改为真实行号——
`profile_selection.py:307/314` → `:65-69/:71-81`，`profile_selection.py:282-385` → `:41-144`，
`registry.py:318-365` → `:66-113`。结论不受影响。

## 10. 后续处置（固化时补记，不属于 C1 原结论）

Codex 独立复核 C1 后作出裁决，由批次 C2a（docs-only）落文：

- F1 + F3 → D-26 / [ADR-0018](../adr/0018-contract-value-semantic-identities.md)（Proposed）
- F5 中有既有依据的部分 → D-27 / [ADR-0019](../adr/0019-lifecycle-evidence-minimum.md)（Proposed）；
  不引入自报职责分离
- F2 → 不改算法，在 `10-migration.md` 写明诚实边界
- F4 → 登记为 D-28 ~ D-31（未决，不选方案）
- F6 → 当前文档已修正；Accepted ADR 正文与历史审查文档不回写
