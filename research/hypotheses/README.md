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
