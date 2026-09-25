# ADR-0045: 策略演化算子与谱系（Phase 12）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 12（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | `research/evolution/`（研究代码，H5；无契约变化） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 演化算子只产生**新版本**：`mutate`（参数只能在已声明的搜索空间内移动，计入 C-T1 的尝试次数）、`combine`（两个父代，
   参数冲突即拒绝）；子代的 `lineage` 指向父代，父代不变；子代从 `IDEA` 重新进入生命周期并重新验证。
2. `require_new_version`：ACTIVE 策略的任何内容变化必须是新版本且谱系可追溯，否则拒绝（roadmap P12 禁止在线就地修改）。
3. `retire` 产生追加式 `RetirementRecord`（RETIRED ≠ FAILED）；`LineageGraph` 追溯祖先 / 后代并报告缺失的祖先。
4. 劣化信号驱动的自动演化调度属 P11 循环，接入 P8 验证后实现；演化本身过拟合近期数据的风险由完整验证与 trial 计数约束。

## Implementation note (durable ledgers, 2026-09-25)

不新增 ADR，不改契约。调试批次的发现：`LineageGraph` 只从构造时传入的 `specs` 在内存中建图，没有任何持久化，进程重启后必须由调用方
重新提供完整的谱系集合才能追溯父子关系。

修复：`LineageGraph.__init__` 新增可选关键字参数 `path`（省略即原有的纯内存行为，完全向后兼容——既有调用都只传位置参数 `specs`）；
新增公开方法 `add(spec)`，把一个规格记入图中（`__init__` 对 `specs` 的合并现在就是逐个调用它）。给出 `path` 时，`add` 在看到一个新
`ref` 时追加一行到 `research.persistence.AppendOnlyJournal`（同 ADR-0040 / ADR-0041 同日实施说明的持久化写法与哈希链）；同一 `ref`
重复 `add` 相同内容是幂等的，不同内容则拒绝（新增异常 `LineageError`：一条谱系关系一旦记录不可覆写）。重新打开同一文件会重放并校验
整条哈希链，恢复全部已记录的规格，因此父子关系跨进程重启持续可追溯。文件被篡改、截断或出现未知记录类型一律
`research.persistence.JournalCorrupted`。

回归测试：`tests/research/evolution/test_durable_lineage.py`（父子关系跨重启持续；重启后 `add` 仍落盘；重放后幂等 re-add 与换内容
拒绝；篡改文件拒绝；确定性重放）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
