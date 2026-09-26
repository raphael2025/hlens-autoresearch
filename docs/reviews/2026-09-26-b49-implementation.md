# B49 修复记录：ProposalAnchor 独立跨进程锁与前缀核对（2026-09-26）

- 分支：`claude/fix-b49-anchor-lock`，基于 `1cd3284`（WIP）；状态 **CODE_COMPLETE / DEBUG_PENDING**，不是 Phase 12 验收
- 依据：[Codex 全代码复核](2026-09-26-codex-full-code-review.md) "B49 Phase 12 外部锚点并发复核" 一节（复核分支 `codex/full-code-review-2026-09-26`，证据提交 `544860d`）的架构决定；本次不做新的架构决定
- 范围：只改 `research/evolution/`（`proposals.py`、`README.md`）与 `tests/research/evolution/`；不触及契约、其他模块或共享计划文档

## 复核发现

两个不同路径的 `ProposalLedger` 共用一个外部 anchor 时，各自持有自己的 `<ledger>.lock`，互不排斥；`ProposalAnchor` 持有构造时缓存的 `AppendOnlyJournal`，`publish()` 从缓存读取并追加。两次 `record()` 都成功返回，anchor 却出现两行 `seq=1`，新建 `AppendOnlyJournal(anchor)` 抛出 `JournalCorrupted: …:2 has the wrong sequence number`。

## 改动

1. `ProposalAnchor` 不再缓存日志。每次 `load` / 发布都在 anchor 自己的 OS `flock`（`<anchor>.lock`，与任何账本锁无关；读用共享锁、写用独占锁，阻塞等待，进程死亡时由内核释放）内，从磁盘重新构造 `AppendOnlyJournal` 并逐行核验：类型与字段形状正确，count 严格递增。
2. **前缀规则**：发布新 head 前，每一个已锚定的 `(count, head)` 都必须等于目标账本第 `count` 行的哈希。账本短于锚点（截断、删除、回滚，或 anchor 属于另一个账本）或在已锚定位置不同（改写，或另一个账本共用 anchor）时抛出 `ProposalLedgerInconsistent`，且不写入任何内容。锚点 count / head 永不回退、不横移。
3. `ProposalLedger.record` 在 anchor 独占锁内按"核对 anchor → 追加账本行 → 锚定新 head"执行，因此被拒绝的 `record` 既不写账本也不写 anchor。重开时的核对（`_check_anchor`）走同一路径。
4. 仍保留合法恢复：账本因"追加后、锚定前崩溃"而领先 anchor 时，重开会从已核实的共同前缀向前补锚。
5. 公开 API 变化（研究层，非冻结契约）：`ProposalAnchor.publish(count, head)` 改为 `publish(chain)`，参数是账本各行哈希的有序序列。旧签名无法核对前缀，因此删除。空 anchor 上的 `publish` 是人工的显式锚定；账本自身永远不会自动接管空 anchor（"lost or attached late" 拒绝不变）。
6. 提案仍只追加、恒为 `PENDING_HUMAN_APPROVAL`；本模块依旧没有批准、编辑或删除路径。

## 回归测试（`tests/research/evolution/test_proposal_anchor_sharing.py`，子进程 `anchor_child.py`）

每个返回成功的调用之后，新建的 `AppendOnlyJournal(anchor)` 都必须能重放：seq 连续，count 严格递增，每个 head 等于账本对应行的哈希，末行等于账本当前 head。

- (a) 两个账本共用 anchor（复核原始复现）：第二个账本被拒且零行；另一个账本 1 / 2 / 3 行时都不能接管 anchor；两个陈旧的 `ProposalAnchor` 对象每次都从磁盘读取，拒绝其他账本的链和落后的前缀；账本打开后 anchor 被他人移动，此时 `record` 拒绝且零写入；anchor 自身回退或出现外来类型的行时 `load` / 发布都拒绝
- (b) 独立进程：4 个进程、4 个不同账本同时 `record` 同一 anchor，恰有一个成功记录 5 条，其余在第一次 `record` 被拒且账本零行，anchor 恰好 5 行；4 个陈旧发布进程以乱序发布同一账本 1..12 的前缀，第 5 个进程发布外来链（全部被拒），最终 anchor 为 `(12, head)`，且每行都在账本链上
- (c) 同一账本三轮重开续记，两次"追加后崩溃"后恢复补锚，恢复后可继续记录；陈旧 anchor 对象读到最新值，并拒绝回退
- 原有测试 `test_the_anchor_never_moves_back_or_sideways` 改用 `publish(chain)`，保留"不回退、不横移"断言（空链、更短、同长不同、更长但分叉均被拒）

在修复前的 `proposals.py`（`1cd3284`）上，新文件 8 项全部失败，失败原因即缺陷本身：第二个账本 `DID NOT RAISE`；4 个进程全部 `recorded: 5`；陈旧 anchor 返回缓存的 `None`。

## 实际运行的命令与结果（本工作树，uv 项目环境，`systemd-run` 内存上限 3G）

| 命令 | 结果 |
|---|---|
| `uv run pytest -q -p no:cacheprovider tests/research/evolution` | 62 passed in 8.01s |
| `uv run pytest -q -p no:cacheprovider tests/research/evolution/test_proposal_anchor_sharing.py -k separate_processes` ×10 | 每次 2 passed, 6 deselected（0.50～0.61s） |
| `uv run pytest -q -p no:cacheprovider tests/test_docs_consistency.py tests/test_architecture_boundaries.py tests/infrastructure/revision/test_repository_hygiene.py` | 25 passed in 0.66s |
| `uv run ruff check research/evolution tests/research/evolution` | All checks passed! |
| `uv run ruff format --check research/evolution tests/research/evolution` | 13 files already formatted |
| `uv run mypy research/evolution tests/research/evolution` | Success: no issues found in 12 source files |

未运行全量 pytest 门禁；全量门禁须在各通道合并后的最终集成 SHA 上运行（见复核记录）。

## 仍然成立的限制

- anchor 只能发现丢失的行，不能认证新增的行（哈希链不是签名），与原设计一致
- 绕过 `ProposalAnchor`、直接写 anchor 文件的进程不受锁约束；它造成的损坏会在下一次 `load` / 发布时被发现并拒绝，但不会被修复
- 锁是阻塞的：持锁进程挂起时，其他发布者会一直等待（每次发布只持锁到一次追加加 fsync 为止）
- ADR-0045 §账本一句未提 anchor 自身的锁；本次未改 ADR（留作 FOLLOW-UP）
