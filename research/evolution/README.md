# research/evolution

Phase 12 Strategy Evolution（[ADR-0045](../../docs/adr/0045-strategy-evolution.md)）。研究代码（H5）；无契约变化。
状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

| 模块 | 内容 |
|---|---|
| `operators.py` | `mutate`（只在声明的搜索空间内移动参数）、`combine`（参数 / 重叠搜索空间 / risk policy / 适用标的冲突即按 ADR-0069 拒绝）、`require_new_version`（拒绝就地修改 ACTIVE）、`retire`（追加式 `RetirementRecord`） |
| `lineage.py` | `LineageGraph`：祖先 / 后代 / 缺失祖先；可选持久（哈希链只追加日志） |
| `proposals.py` | 替换提案：`propose_replacement`、`ReplacementProposal`、`ProposalLedger`（单写者锁、可选外部锚点 `ProposalAnchor`） |
| `replacement_job.py` | 替换提案作业 `propose_replacements`（循环之外；见下） |

后代是新版本、从 `IDEA` 重新进入生命周期并重新验证；父代不变。循环中的调度见 `research/loop/evolution.py`。

## 替换提案（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

演化只能**提出**"用已重新验证的后代替换运行中的策略"，从不批准、晋升或替换。`propose_replacement` 先核对，再记录：

- 现任策略的生命周期历史属于它，且处于 `ACTIVE` 或 `DEGRADED`；
- 候选是新版本且源自现任（`require_new_version`），谱系图能从候选追溯到现任，候选的所有祖先都已记录、候选内容与谱系中记录的一致；
- 候选在**自己的**历史上重新通过验证：处于 `PAPER` 或 `PRODUCTION_CANDIDATE`（不从父代继承状态）；
- 至少一项非空证据引用（如验证报告哈希）、非空理由与提出者；时间戳必须带时区。

结果 `ReplacementProposal` 的 `status` 恒为 `PENDING_HUMAN_APPROVAL`（构造参数里没有 `status`，载荷里写其他值即拒绝），
`proposal_hash` 覆盖全部载荷；执行提案是人在正常 Promotion 路径上的生命周期转移（ADR-0005 / ADR-0006），不在研究平面。
`ProposalLedger(path)`：哈希链只追加日志，重开时逐条重建并复核哈希；相同提案重复记录不追加；没有批准 / 编辑 / 删除方法，
未知记录类型或写入"已批准"的行一律 `JournalCorrupted`。

已知限制：`require_new_version` 拒绝与现任**版本号相同**的候选（即使名称不同），因此 `combine` 产出的 `1.0.0` 不能替换 `1.0.0` 的现任——保守，保持不变。
测试：`tests/research/evolution/test_replacement_proposals.py`。

## 替换提案作业（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`replacement_job.py`：`propose_replacements(...)`，由调用方显式运行的**研究作业**，不在持续循环内。原因：循环的生命周期护栏最多到 `OOS`
（`OOS → PAPER` 需人工批准），循环里的后代永远不会是 `PAPER` / `PRODUCTION_CANDIDATE`，没有循环内触发点；循环保持不变（记录与指纹逐字节不变）。

- 输入全部来自循环之外：`Incumbent(spec, history)`（生产侧声明，`ACTIVE` / `DEGRADED`，否则构造即拒绝）、
  `ReplacementCandidate(spec, history, report_hashes)`（人工 Promotion 路径记录的生命周期 + 支撑它的报告哈希）、报告解析器
  （`research.router.evidence.report_store_resolver` 或 `reports_by_hash`）、`read_lineage(state_dir / LINEAGE_FILE)`（循环持久谱系的已校验内存副本，只读，
  不写循环目录）、`ProposalLedger`、理由模板（`{incumbent}` / `{incumbent_state}` / `{candidate}` / `{candidate_state}`）、提出者、时间，均无默认值。
- 每个（候选，现任）对：非后代 → `not_descendant`；账本已有 → `already_proposed`（跨运行与重启幂等）；候选不在 `PAPER` / `PRODUCTION_CANDIDATE` →
  拒绝；每个声明的报告都须通过 `check_report`（存在、格式正确、哈希一致、`subject` = 候选、PASS **且含 G5**）；再经 `propose_replacement`；
  证据为 `validation_report:<hash>`。拒绝写入结果的 `refused`，从不抛出；提案恒为 `PENDING_HUMAN_APPROVAL`，不改变任何生命周期。
- `ProposalLedger`：单写者（`<path>.lock` 的 `flock`，`ProposalLedgerLocked`）；可选外部锚点 `anchor=`（`ProposalAnchor`，须在账本目录之外）：
  重开时账本少于锚点（尾部删行、删除、回滚）、锚点位置另有一行、或锚点为空而账本有行 → `ProposalLedgerInconsistent`；账本领先锚点（追加后锚定前崩溃）
  被接受并锚定。无锚点时整行尾部截断仍无法发现（已记录的限制）；锚点发现丢失，不认证新增。
- `ProposalAnchor`（B49）：一个锚点只属于一个账本，不是多账本汇总器。锚点有自己的跨进程 `flock`（`<anchor>.lock`，与任何账本锁无关；读共享、写独占），
  每次 `load` / 发布都在锁内从磁盘重新构造并逐行核验 `AppendOnlyJournal`（无缓存）。发布新 head 前，每个已锚定的 `(count, head)` 都必须等于目标账本
  第 `count` 行的哈希；账本短于锚点或在已锚定位置不同（回滚、改写、或另一个账本共用同一锚点）即拒绝，且被拒绝的 `record` 既不写账本也不写锚点。
  `record` 在锚点锁内完成"核对 → 追加账本行 → 锚定"。锚点 count / head 永不回退、不横移；账本因崩溃领先锚点时重开仍会补锚。
- `propose_replacement` 的谱系检查只要求**策略**祖先完整：库策略的 `lineage` 同时引用其来源知识条目（`Kind.KNOWLEDGE`），它们不是策略版本，
  从不在策略谱系图中（循环的演化阶段同样只检查策略链接）；缺失的策略祖先仍被拒绝。

测试：`tests/research/evolution/test_replacement_job.py`（含对真实循环状态目录的只读运行）、`tests/research/evolution/test_replacement_proposals.py`。
