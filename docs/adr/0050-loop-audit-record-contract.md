# ADR-0050: 持续研究循环审计记录的版本化契约（Phase 11，只追加）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 11（Continuous Research Loop）；调试批次 backlog P11"审计记录尚不是版本化契约" |
| 影响范围 | Contract（新增 `core/contracts/loop_audit.py` 的 8 个模型与 Schema）；`apps/worker/loop.py`、`apps/api/store.py` / `app.py`、`research/reports/loop.py` |
| 是否破坏兼容 | 否：只追加；既有模型、Schema 与既有审计记录的字节和 `record_hash` 都不变 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 背景

[ADR-0049](0049-continuous-research-loop.md) 的循环每轮写一条哈希链审计记录（`LoopRecord`）：进 worker 的持久日志
（`loop_round_started` / `loop_round_recorded` 行）、发布到总线，并经 `research/reports` 写成控制台的
`research_loop_round` 报告。这些载荷的形状只存在于 `apps/worker/loop.py` 的 `payload()` 方法里，没有 Schema、
没有 `schema_version`，控制台按不透明 JSON 读取——形状漂移不可见（ADR-0049 后果："`LoopRecord` 尚非版本化契约"）。
另外，worker 重放日志时只校验字段类型与哈希，**不**校验阶段顺序、状态与阶段是否一致、用量是否相加；一个
按全部哈希规则重算过哈希的篡改记录（例如打乱阶段、改轮次状态）可以通过重放。

约束：H1（契约只追加，既有模型 / Schema 逐字节不变）；审计记录的 `record_hash` 已被日志、检查点、外部锚点与
报告文件名引用，**不得**改变哈希或字节；worker 只依赖 `core` 与标准库（ADR-0049 §1，测试静态检查）；
`apps/` 不得 import `research/`。

## 裁决

1. **契约描述既有字节**。新增 `core/contracts/loop_audit.py`，8 个 `Contract` 模型逐字段镜像已持久化的形状：
   `LoopBudgetUsage`（用量）、`LoopBudgetLimits`（`LoopBudget.payload`，其哈希即记录中的 `budget_hash`）、
   `LoopStageRecord`、`LoopTransitionRecord`（`from` / `to` 通过别名保留原键）、`LoopOverrun`、`LoopRoundRecord`、
   `LoopRoundStarted`、`LoopRoundRecorded`（日志两类行）。追加到 `CONTRACT_MODELS` 末尾（126 → 134），
   Schema 由 `python -m core.contracts.registry` 导出。不升 `CONTRACT_SCHEMA_VERSION`，不新增 `Kind`。
2. **没有信封的持久字节**。模型带 `schema_version`（默认当前版本），但持久化的审计载荷从未携带它：
   `audit_payload()` 重建**不含** `schema_version` 的原形状，`from_audit_payload()` 要求载荷经模型往返后规范 JSON
   **逐字节相同**，否则拒绝（多出 `schema_version` 键同样拒绝）。`LoopRoundRecord.record_hash` 仍是 worker 的规则
   `content_hash(payload)`；它**不是** `Contract.content_hash()`（后者含信封，02-domain.md §3.1）。
   在持久载荷中加入 `schema_version` 会改变全部 `record_hash`，是破坏性变更，须另立 ADR 与迁移说明。
3. **不做空白规范化**。这些模型关闭 `str_strip_whitespace`：空白属于被哈希的字节，要么原样接受、要么拒绝；
   来自已去空白生命周期对象的字段（`reason`、`evidence`、`triggered_by`）带首尾空白即拒绝。
   worker 以 `str(Decimal)` 写的数字必须是该规范文本（有限、非负）。
4. **单条记录可判定的不变量进入契约**（都是 worker 构造记录的方式，不是新规则）：阶段顺序（`STAGE_ORDER` +
   可选阶段固定位置；`check_stage_order` 与三个顺序常量移入契约模块，worker 原样再导出）；状态枚举；每个阶段
   只携带其状态蕴含的字段；overrun = 实际 − 声明；`charged` = max(声明, 实际)（仅当不同于实际）；轮次状态 = 第一个
   未完成阶段的结果、其后全部 `SKIPPED`；轮次 `overrun` = 第一个超支阶段；`round_usage` = 各阶段计费之和；
   `total_usage` 覆盖 `round_usage`；只有第 0 轮 `previous_hash` 为空；转移必须是生命周期图中的边且不是人工审批边；
   记录行的 `record_hash` 重算核对。跨记录规则（链、累计、计划时刻、预算绑定、护栏重放）仍由 `LoopAuditLog` /
   `ResearchLoop` 负责。
5. **所有读写点都 fail closed**。
   - worker：`LoopAuditLog.begin_round` / `append`（持久或内存）写入前校验；重放每一行时先核对存储的
     `record_hash`，再按契约校验，再重建。写入被拒 → 该轮不记录、循环 `stopped`；重放被拒 → `LoopAuditCorrupted`。
   - `research/reports` 的写入器：写前校验，被拒则什么都不写。
   - `apps/api` 的 `ReportStore`：`research_loop_round` 文件必须是合法 `LoopRoundRecord` 且文件名等于其
     `record_hash`，否则视为 malformed（`list` 跳过、`get` 拒绝，HTTP 422）。其他报告种类仍不透明地提供。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 契约描述既有字节（本 ADR） | 不改任何哈希；旧日志与报告直接可校验 | 持久字节里没有 `schema_version`，版本只在 Schema 侧 | 采用 |
| B. 在持久载荷里加入 `schema_version` | 字节自带版本 | 改变所有 `record_hash`，破坏已锚定的日志 / 检查点 / 报告 ID | 破坏性，需迁移 |
| C. 只导出 Schema，不在读写点校验 | 改动最小 | 漂移与篡改仍不可见 | 不满足 fail closed |

## 后果

- 正面：审计形状有版本化 Schema，漂移会让 Schema 导出测试失败；打乱阶段、改状态、改用量后重算哈希的记录在
  重放、报告写入与控制台读取时都被拒绝。
- 负面 / 代价：stage 摘要必须是键为字符串的 JSON 对象（此前非字符串键会被 JSON 静默转成字符串而通过；现在该轮
  不被记录、循环停机）；控制台 fixture `research_loop_round/` 原来是一条没有阶段的记录（worker 不可能写出），
  已按写入器重新生成。
- 需要迁移的内容：无（既有字节不变）。
- 对复现性的影响：无；同种子同输入的 `record_hash` 不变（测试覆盖）。

## 合规检查

- [x] 不破坏已冻结契约：只追加 8 个模型；既有模型与 Schema 逐字节不变
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖（契约模块只依赖 `core` 与 pydantic）
- [x] Research / Application Plane 边界不变（worker 仍只依赖 `core` 与标准库；`apps/` 不 import `research/`）

## 参考

- [ADR-0049](0049-continuous-research-loop.md)、[02-domain.md §2.10](../architecture/02-domain.md)、
  `core/contracts/loop_audit.py`、`tests/test_loop_audit_contract.py`、`tests/apps/test_reports.py`
