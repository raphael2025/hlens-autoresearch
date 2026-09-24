# ADR-0020: 发布 Research Constitution 1.0.0

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24；待 Codex 复核文本后依已记录的授权执行批次 C4b） |
| 日期 | 2026-09-24 |
| 决策者 | Raphael（2026-09-24 持续授权，范围与限定见「授权记录」）；Codex 依该授权复核并执行 |
| 起草者 | Claude Code（Opus），批次 C4a |
| 相关 Phase | Phase 0（关闭序列） |
| 影响范围 | Constitution（版本与状态，不含原则变化）/ 治理 |
| 是否破坏兼容 | 否：原则正文零变化；契约、Schema、代码均不改 |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0018](0018-contract-value-semantic-identities.md)、[ADR-0019](0019-lifecycle-evidence-minimum.md) |
| 依据 | [Phase 0 关闭复验 C3](../reviews/2026-09-24-phase0-closing-review-c3.md)：`READY_FOR_HUMAN_CONSTITUTION_GATE` |

## 背景

roadmap Phase 0 的第一项输出与验收标准是"批准的 Constitution v1.0.0（纯原则）"。
`docs/research/constitution.md` 目前是 `0.2.0-draft / Draft`，页首写明"批准前不得用于判定任何实验"。

C3 修复后关闭复验（基线 `4a2951a`）的结论是 `READY_FOR_HUMAN_CONSTITUTION_GATE`：无 P0 / P1 / P2；
Constitution 全文是纯原则、无数值阈值，自 C1 基线 `fce4f81` 起 0 行改动，并与当前契约和 ADR 一致；
唯一未满足的 Phase 0 验收项就是这道批准门。C3 同时指出两处**可选**措辞澄清（A6 的资金费率、C-G2 的批准集合表述），
并判定它们不阻塞批准。

## 授权记录

- **来源**：Raphael 于 2026-09-24 在当前协作会话中明确表示"授权所有，你全权接管并开发、测试、决策并查看和更新相关文档"，
  并授权 Codex 全权接管项目决策、开发、测试、相关文档与 Git 版本控制。
- **Codex 的解释**：该持续授权覆盖**原则正文零变化**的 Constitution 1.0.0 状态 / 版本发布，
  以及随后 Phase 0 的正式关闭、`phase/0` → `main` 的 fast-forward 合并与轻量 tag `phase-0-complete`。
- **限定**：
  - 仅在本 ADR 的发布方案保持"原则正文零变化、无任何数值阈值"时适用；只要出现任何原则或阈值变化，
    本授权即不适用，必须就该具体变化重新取得 Raphael 的批准；
  - 不覆盖实盘交易、资金、风险预算或任何 Phase 13 事项；
  - 不开启 Phase 1 或其它 Phase。
- 本 ADR 暂为 Proposed，只是为了保留 **Proposed → 独立复核 → Accepted** 的审计顺序，**不表示**缺少 Raphael 授权，
  下一步也不需要 Raphael 再次答复。

## 决策

### 1. 版本与状态发布

`docs/research/constitution.md` 从 `0.2.0-draft / Draft` 发布为 **`1.0.0 / Approved`**。只允许三处改动：

| 位置 | 改动 |
|---|---|
| 页首「版本」行 | `0.2.0-draft` → `1.0.0` |
| 页首「状态」行 | 改为 Approved，引用本 ADR，写明只前向适用；删除"批准前不得用于判定任何实验"这一过渡性表述 |
| 「修改历史」表 | 追加 `1.0.0` 一行：日期、ADR-0020、"批准发布；原则与阈值均无变化" |

### 2. 原则正文零变化

第一章 ~ 第九章的原则正文**逐字不变**；页首「修改规则」行、「不包含任何数值阈值」行与开头引言也不变。
C3 提到的可选措辞澄清**不**在本 ADR 中处理：它们是原则层文本变化，超出授权限定，
如需采纳必须另起 ADR 并取得 Raphael 对具体变化的批准，发布后按第九章只前向生效。

发布前基线：`sed -n '/^## 第一章/,/^## 修改历史/p' docs/research/constitution.md | sha256sum`
= `4d603d6228541d8c09abb068f5f2162e096b061c20afce73ad4d3e3b4e5259cd`（在 `4a2951a` 上计算）。
C4b 必须在改动后得到同一哈希。

### 3. 不含数值阈值

1.0.0 不加入任何数值阈值。D-09 的 TBD-1 ~ TBD-5 与 H-3 ~ H-7 保持开放；Validation Profile 的参数仍按
ADR-0007 两步冻结的 Step 2，在 Phase 4 校准后独立冻结，不因本 ADR 提前。

### 4. 只前向适用

- 1.0.0 只适用于**批准之后**预登记的实验（第九章第 3 条、原则 A5）。
- 批准之前没有任何实验可依据该宪法判定，仓库内也没有任何已登记实验；不存在追溯重判的对象，亦不得追溯重判。
- 未来实验在复现元组中绑定 `constitution_version = "1.0.0"`。测试夹具中出现的 `0.2.0-draft` 只是测试数据，
  不需要随本 ADR 修改。

### 5. 这是治理发布，不是运行时能力

发布 1.0.0 只改变宪法的**治理状态与版本**。它**不**表示验证流水线、泄漏门、多重检验校正、trial 权威账本、
Profile 选择服务或任何 Registry / Runner / Control Plane 已经实现；这些仍按各自 ADR 与 roadmap Phase 延期。

### 6. 顺序门

以下四道门**分别**执行、分别留下可恢复记录，后一道以前一道完成为前提：

| 顺序 | 门 | 批次 | 产物 |
|---|---|---|---|
| 1 | 本 ADR Accepted，Constitution 发布 1.0.0 | C4b | 一个 docs-only 提交 |
| 2 | Phase 0 正式关闭（状态文档收口） | C5 | 一个 docs-only closure 提交 |
| 3 | `phase/0` → `main` 合并 | C5 | 仅 fast-forward；不产生 merge commit、不 rebase、不改写历史 |
| 4 | 轻量 tag `phase-0-complete` | C5 | 指向合并后的 `main` HEAD；不移动、不覆盖任何 tag |

每道门前都实际运行四项工程检查；仓库无远程，因此不 push、不声称 PR / CI。

## 明确不做

- 不改动任何原则、不加入任何数值阈值、不采纳 C3 的可选措辞建议。
- 不改动代码、测试、Schema、Accepted ADR 正文、C1 / C3 历史报告。
- 不开启 Phase 1；不决定 D-01、D-02、D-04、D-08、D-10、D-28 ~ D-31、H-3 ~ H-7、Q-1 ~ Q-7。
- 不涉及实盘、资金或风险预算。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 原则零变化发布 1.0.0，四道门顺序执行 | 满足 roadmap Phase 0 验收；原则已经 C1 / C3 两轮复审；审计链清楚 | 可选措辞澄清留待以后另起 ADR | — |
| B 不批准，继续 draft | 无需任何动作 | Phase 0 无法关闭；Phase 1+ 无可用的裁判规则；C3 未发现阻塞内容 | 没有支撑它的具体问题 |
| C 先修改原则（如 A6 措辞）再批准 | 文本更精确 | 原则变化超出授权限定，需要 Raphael 对具体变化另行批准；C3 判定不阻塞；可在发布后按第九章前向修改 | 把非阻塞优化变成关闭阻塞 |
| D 在 1.0.0 中写入数值阈值 | 看似更"可执行" | 违反 ADR-0007 三层结构与两步冻结；阈值未经 Phase 4 校准 | 违反已接受的决定 |
| E 批准、关闭、合并、tag 合并成一步 | 步骤少 | 失去逐门复核与恢复点；任何一步出问题难以定位 | 审计性下降 |

## 后果

- 正面：Phase 0 的最后一项验收标准有了明确、可复核的批准记录；此后预登记的实验有确定的裁判原则版本。
- 负面 / 代价：原则一经 1.0.0 发布，任何修改都必须走第九章程序并只前向生效；可选措辞澄清需要一份新 ADR。
- 对复现性的影响：不改变任何契约、Schema 或哈希；v1 只读路径不受影响；未来复现元组绑定 `1.0.0`。

## 验收矩阵（C4b 执行时逐项核对）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 第一章 ~ 第九章正文哈希（上文命令） | 改动前后都等于 `4d603d62…259cd` |
| 2 | `git diff` 限于 `constitution.md` 页首版本行、状态行与修改历史新增 1.0.0 行 | 是；其余行零差异 |
| 3 | Constitution 无数值阈值 | `tests/test_docs_consistency.py` 的数值检查通过，人工复核无新数字 |
| 4 | 所有 Markdown 链接可解析、代码块闭合、ADR 索引与 ADR 状态一致 | 文档一致性测试通过 |
| 5 | 完整 pytest、ruff check、ruff format --check、mypy | 实际运行且全绿 |
| 6 | 本提交只改 `.md` | 是；代码、测试、Schema、v1 冻结资产零差异 |
| 7 | ADR 索引、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、README、roadmap | 一致写为 Constitution 1.0.0 已发布、Phase 0 尚待关闭 / 合并 / tag |
| 8 | 本 ADR 状态与授权记录 | Accepted，并如实记录 Raphael 授权来源与限定；不写"Phase 0 已关闭" |
| 9 | 其它 Accepted ADR 正文与 C1 / C3 历史报告 | 零差异 |

## 合规检查（Proposed 阶段）

- [x] 原则正文零变化，不加入任何数值阈值（ADR-0007）
- [x] 只前向适用，不追溯重判
- [x] 不宣称任何运行时验证能力已实现
- [x] 授权来源与限定已记录；超出限定的变化不适用该授权
- [ ] Codex 复核本文本 —— 待进行
- [ ] 验收矩阵 1 ~ 9 —— C4b 执行时核对
