# ADR-0040: 假设生成、组合算子、预登记账本与 LLMProvider 契约（Phase 7）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 影响范围 | Contract（`core/contracts/llm.py`，additive）、`plugins/llm/`、`research/hypotheses/` |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `LLMProvider`：`complete(LlmRequest) → LlmResponse`，每次调用登记为 `LlmCall`（ADR-0016，提示 / 输入 / 输出内容寻址）；
   descriptor 声明模型、是否确定性、是否联网。首个实现 `ScriptedLLMProvider` 离线、确定性（测试与演练）；真实联网 Provider
   需凭据，属后续批次。
2. 组合算子（04-research-loop.md §4：条件化、交互、时序、变换、集成、反向对照）只产出**规格**，`origin = combination`，
   上游引用写入 `origin_refs`；生成内容永不作为代码执行（roadmap P7 禁止事项）。
3. `TrialLedger`：假设运行前登记、登记后不可改（同一 `name@version` 换内容即拒绝）；每次登记计入所属假设族的 trial 数
   （含失败，Constitution A3 / C-T1），重复登记同一内容不重复计数；只增不删。
4. 生成：`from_knowledge`（每个知识主张一个待检验假设，`origin = knowledge`）；`from_llm`（输出须通过结构校验，结果是
   `reviewed = False` 的草稿，账本只接受经人工审阅的草稿）。LLM 不参与任何裁决、不修改验证规则。
5. 批量实验调度复用 `apps/worker`（ADR-0044）；trial 校正在 P4 / P8 验证中消费账本计数。

## Implementation note (durable ledgers, 2026-09-25)

不新增 ADR，不改契约。调试批次的发现：`TrialLedger` 只存在于内存，进程重启后一个族的 trial 计数回落到 0，破坏 C-T1 的多重检验
校正（校正用的尝试次数必须是该族**全部**已登记假设，失败的也算）。

修复：`TrialLedger.__init__` 新增可选参数 `path`（省略即原有的纯内存行为，完全向后兼容——所有既有调用都是 `TrialLedger()` 无参）。
给出 `path` 时，每次**新**登记（`register` / `register_draft` 返回 `True`）追加一行到 `research.persistence.AppendOnlyJournal`
（哈希链、只追加、`flush` + `fsync`，与 `research.strategies.failure_registry.FailureRegistry` 同一持久化写法，另加 SHA-256 哈希链，
细节见 ADR-0041 同日实施说明）；重复登记同一内容不追加（保持幂等，不重复计数）。重新打开同一文件会重放并校验整条哈希链，恢复
`_registered` / `_order`，因此一个族的 trial 计数跨进程重启延续；换内容重复登记（`name@version` 相同、内容不同）在重放时与实时注册
一样被拒绝（`LedgerError`），文件被篡改、截断或出现未知记录类型一律 `research.persistence.JournalCorrupted`。

回归测试：`tests/research/hypotheses/test_durable_ledger.py`（trial 计数跨重启延续；重放后幂等登记与换内容拒绝；篡改文件拒绝；
确定性重放同一状态）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
