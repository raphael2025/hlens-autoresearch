# Phase 1 D3A 验收记录：ADR-0027（REST Raw / 通道等价 precedence）

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 复核者 | Codex（Raphael 2026-09-24 "授权所有" 的持续授权范围内） |
| 落文 | Claude Code（Opus），接受门（docs-only） |
| 复核对象 | D3A 草案 `7111e54`（退回）；D3A-R1 修正 `ed526f7`（接受） |
| 结论 | **PASS — ADR-0027 ACCEPTED; D3B OPEN** |

## 1. 复核经过

- `7111e54`：Codex 复核退回，共八项缺陷（F1～F8）。
- `ed526f7`：逐项修正，并把拓扑从三张改为四张 additive Raw 表；Codex 独立复核接受。

## 2. 已关闭的八项缺陷

| # | 缺陷 | 关闭方式（ADR-0027） |
|---|---|---|
| F1 | 目标窗口与页面合法性互相矛盾 | §6：collection target window 只用于停止与覆盖账；page validity envelope 由实际查询、排序、字段形状、声明单位、`retrieved_at` 上界决定，零容差；越出目标窗口的合法元素照常入 Raw |
| F2 | response revision 被写成没有 `knowledge_time` | §8：每个完整收到的页都是带必填 `knowledge_time` 的 revision；`ingest_time` = 末字节；decoder 失败只阻止元素层 |
| F3 | 本机预算耗尽可能冒充来源缺口 | §7：预算、限流、5xx、传输、截断、无效 JSON、envelope 违约均为 `CollectionFailed`；`SOURCE_ABSENT` 只用于短页 / 空页后的尾部，detail 只陈述确切查询的回答 |
| F4 | `CollectorAdapter` 重放义务未满足 | §5 / §9：逻辑 `request_id` 与规范页身份分离；不可变写一次 checkpoint；三个崩溃点；必需协议不变 |
| F5 | `arrival_seq` 守卫与"identity.py 不动"冲突 | §11：`identity.py`、`IDENTITY_HASH`、D2 store 不改；REST 自 `2**62` 起；只在单个被聚合 graph 内做 range guard |
| F6 | 批次与设置不完整 | §12：复用 HTTP 超时 / 重试 / UA / base URL，新增四项 REST 设置；roadmap D3B～D3E 按四表方案重拆，decoder 先于 collector |
| F7 | 证据"两个独立渲染路径"措辞过度 | 证据文件改为"两种官方访问 / 渲染路径交叉核对同一规范" |
| F8 | 状态与索引不一致 | 状态、记忆、ADR 索引同步；ADR-0026 标为已实施 |

## 3. 四表方案与 D-33

- 冻结表内嵌的 `precedence_evidence` 省略外侧新端（隐含"新端 = 本行"），REST 后到时无法诚实表达"归档取代 REST"。
  因此新增 `raw.binance_spot_precedence_evidence`：独立、append-only，保存完整 `PrecedenceEvidence` 两端，`edge_id` 不含时间，
  首次提交即权威、重放复用，任何已提交 revision 行都不改写。另三张为 REST 响应页与两张元素表。
- **D-33 方案 A 生效**：项目定义的版本化规范内容投影逐字段相等时，写 evidence-only 边"归档 revision 取代 REST revision"；
  不等 / 缺字段 / 超定义域 / 不可比较 → 无边、competing heads、fail closed。它是项目政策，不是 Binance 声明的先后。
  边的 `knowledge_time` 不回填；两种到达顺序的 cutoff 分段结果已写明；收敛由可重跑的 D3E reconciler 保证。

## 4. 独立验证证据

- Codex 对 `ed526f7` 的独立复核：设计 PASS；D3A-R1 提交时的检查为真实 PostgreSQL 全量 3091 passed（0 skipped）、
  ruff check / ruff format --check / mypy strict / `uv lock --check` 全绿。
- 接受门提交在同一基线上重跑：`git diff --check` 无输出；docs 一致性 7 passed；ruff check、ruff format --check、mypy、
  `uv lock --check` 全绿；加载本机忽略的测试 catalog 凭据后真实 PostgreSQL 全量 3091 passed。docs-only，测试数不变。
- 冻结边界：八张表定义、既有标识符与设置、`core/` 契约、Schema 导出、依赖、Constitution 均未改。

## 5. 残余风险

- REST 1.0.0 以毫秒交付，2025-01-01 起归档为微秒：带非零亚毫秒位的 aggTrade 无法证明内容相等，D-33 下一律 fail closed。
  这是正确结果，但降低 REST 补尾对近期 aggTrade 的价值；改请求微秒须新的页身份 / decoder 版本、只读 smoke 与 Codex 批准（ADR-0027 §4.8）。
- 实施批次须按证据文件 §3 重新核对官方资料；设计尚无代码验证。

## 6. 结论与下一步

**PASS — ADR-0027 ACCEPTED; D3B OPEN。** 只开放 D3B（四表定义、REST 身份规则、REST policy 与通道等价纯函数，无 HTTP、无写入）；
D3C、D3D、D3E 保持关闭，逐批经 Codex 验收后依次开放。
