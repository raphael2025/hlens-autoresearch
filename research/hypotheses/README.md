# research/hypotheses

假设登记、组合/变换算子（04-research-loop.md §4–5）。

> 框架已实现（ADR-0040，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`dsl.py` 组合算子、`ledger.py` 预登记与 trial 计数、`generator.py` 知识 / LLM 生成（LLM 草稿须人工审阅）。

## 落盘的 TrialLedger（调试批次，2026-09-25，ADR-0040 实施说明）

`TrialLedger()` 省略 `path` 时行为不变（纯内存，进程结束即丢）。传入 `TrialLedger(path=<文件>)` 后，每次新登记
（`register` / `register_draft` 返回 `True` 时）追加一行哈希链 JSON（`research.persistence.AppendOnlyJournal`，
与 `research.strategies.failure_registry.FailureRegistry` 同一持久化写法：只追加、`flush` + `fsync`、文件变短即拒绝）；
重新打开同一文件会重放并校验整条哈希链，恢复已登记的假设与各族 trial 计数——某族的 trial 数因此跨进程重启延续，
不会在重启后回落到 0。同一 `name@version` 重复登记仍是幂等的（不重复计数）；换内容登记仍拒绝（`LedgerError`）。
文件被篡改、截断或出现未知记录类型一律 `research.persistence.JournalCorrupted`，不静默修复。

`ledger.py` 的 `register_reevaluation(hypothesis, attempt)`：已登记假设的再次评估（例如循环在增长的累计研究数据上重新评估 INCONCLUSIVE 假设）作为**单独的 trial** 预登记并计入族 trial 数（`trials` / `trial_index` / `trial_log`；ADR-0049 accumulated validation window 实施说明）。

## 严格的 LLM 草稿与可审计的拒绝（Phase 7 补全，2026-09-26）

`generator.py` 的草稿结构是严格的：多余的键被拒绝（不再静默丢弃），字段类型不做强制转换（数字不是陈述、字符串不是条件元组）。
输出不符合结构、或字段构不成合法 `Hypothesis`（如名称不合命名规则）时抛出 `LlmDraftRejected`（`ValueError` 子类），
其 `call` 是该次交换的 `LlmCall`；循环的 `HypothesisStage` 把被拒调用的内容哈希与 prompt / input / output 引用连同拒绝原因
写进该轮 hypothesis 阶段摘要（`llm.call_hash` / `llm.call`）——LLM 输出无论接受与否都有记录（roadmap P7）。
只有出现被拒 LLM 输出的轮次记录会变；其他记录逐字节不变。

## 声明式假设批次（`batch.py`，Phase 7 补全，2026-09-26）

`BatchGrid`（名称、族、`created_at`、算子、输入 `StrategySpec`、参数点、最小有意义效应——全部必填、无默认值）× `ReviewedOperators`
（声明的、带 SemVer 版本与审阅人的算子白名单）→ `expand_batch` → `HypothesisBatch`（假设由构造计算，不能传入）。每个单元的条件恰好是循环
`trial_point` 能运行的两种形式；以下情况一律 `BatchRefused`，发生在任何登记与运行之前：算子不在白名单上（或同名同版本但内容不同）、
算子种类是 `trial_point` 跑不了的 DSL 算子（conditioning / interaction / temporal / transformation / ensemble / negation）或未知种类、
参数未声明搜索空间、值不在搜索空间内（类型也须一致）、浮点值、文本值读回后不是它自己、空或重复的因子。`preregister_batch(batch, ledger)`
全有或全无地把整批预登记进 `TrialLedger`，族 trial 数因此覆盖整个网格。算子只是数据（主张、方向、固定列表中的种类），生成物永不作为代码执行。

网格中的参数点在构造时会复制并递归冻结。循环以已记录的 `TrialOutcome` 判断单元是否完成，而不是把 TrialLedger 的预登记状态误当作执行完成；Hypothesis 阶段失败后，已预登记但尚无结果的单元仍会在后续轮次按原身份调度，已产生结果的单元不会重复运行。

## 知识检索来源（`generator.py`，Phase 7 补全，2026-09-26）

`KnowledgeSource(provider, query).search(family_id)` 调用 `KnowledgeProvider.search(KnowledgeQuery)`，重新校验结果并拒绝其他查询 / 其他 provider 的结果，
返回 `KnowledgeSearch`（provider、`query_hash`、`result_hash`、条目、`from_knowledge` 生成的假设）；查询哈希与 `result_hash` 是这些假设的来源，
由调用方（循环的 hypothesis 阶段）记入摘要与生命周期证据。
