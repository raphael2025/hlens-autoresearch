# E1 normalizer：已提交计划只保留计数（实现记录）

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-27 |
| 分支 | `codex/e1-normalizer-bounded-2026-09-27`（基线 `c64d1ff`，已含单次 metadata load 的 history 遍历） |
| 范围 | 仅 `infrastructure/canonical/normalizer.py`（另加本记录） |
| 状态 | **IMPLEMENTED / UNTESTED**：只运行了静态检查；按 Raphael 当前指示，没有添加或运行测试，也没有运行容量探针 |
| 与 E1-CAP-1 的关系 | **不关闭 E1-CAP-1**；32 MiB 门槛不变，`docs/reviews/2026-09-27-e1-review.md` 的失败结论不变 |

## 1. 改动

1. **`_CommittedPlan`** 从 `unit_rows / chunk / batches: Mapping[int, SnapshotInfo]` 改为三个 int：`unit_rows / chunk / count`。
   不再保留每个 batch 的 `SnapshotInfo`。
2. **`_committed_plan`** 只遍历一次 pinned history（经 `_unit_batches` 流式解析 batch id），只保存若干 int：
   第一个计划、是否混入其他计划、计数、最新 index、上一个 index、首个重复 index，以及是否按序。返回 `(plan, ordered)`。
3. **`_plan_snapshots`** 按需定位 snapshot：再次流式遍历同一个 pinned head，重新证明计划一致、index 依次为
   `count-1 … 0`，只 yield 调用方要的 index。所需 index 全部取到后立即停止。任何偏离都抛出 `CatalogIntegrityError`。
4. **`_survey_unit`** 从新到旧逐 batch 取 snapshot，并照旧做 `check_batch_snapshot`（fingerprint、row count）、
   `_check_committed_window`；replay 时还做 `_check_unique`。每个 batch 只保留输出所需的 revision ids 或 rows，最后再反转成
   position 顺序。它同时对已核对的 `(index, snapshot_id, batch_id, fingerprint, added_rows)` 计算 sha256 摘要
   （`_Survey.committed_digest`，O(1)）。
5. **`_write` / `_replayed_commits`**：已提交的 prefix 由 `plan.count` 表示，不再复制成 `done` 字典。对这些已跳过的 batch，
   代码再次流式定位 snapshot，核对 `added_rows` 与窗口行数一致，并要求重新计算的摘要**逐字节等于**证明阶段的摘要。
   因此每个被报告为 `ALREADY_COMMITTED` 的 snapshot，都必须是证明阶段已按重读 Raw 行核对过 fingerprint 和 row count 的那一个。
   随后照旧从 `count` 开始补写缺失的 batch。
6. **`_verify_batches`**（`arrival_seqs` 路径）：只为本次需要证明的 batch 定位 snapshot（排除 frozen cache 已命中的 batch）。
   cache 命中项在循环前取出，避免循环内插入导致淘汰后缺少 snapshot。`& set(plan.batches)` 改为 `index < plan.count`；在
   `_require_complete` 通过后两者是同一集合。
7. **`_check_plan`**（模块函数）集中了原先两处重复的判定：Raw 单元大小、contiguous prefix、beyond plan，顺序与原代码相同。
   **`_require_complete`** 改用 `plan.count`。

没有修改：`CanonicalUnitNormalized`（`revision_ids`、`commits` 仍原样返回）、`rules.py`（normalizer 规则哈希不变）、
`row_integrity.py`、catalog adapter、core 冻结契约、Profile、API、32 MiB 门槛、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、总计划。

参考了 `codex/e1-bounded-scan-integration` 的 `_CommittedPlan(count)`、`_plan_walk` / `ordered_batches` 与 `_plan_snapshots` 思路。
由于只能改本文件，没有移植 `row_integrity.ordered_batches`，而是在 normalizer 内实现同样的按序判定；也没有移植该分支中改变
返回类型（`row_count / batch_count`）、spool、`_tiles`、`limit=1` 恢复等跨模块或会改变 API 的行为。

## 2. 完整性保持（逐项）

| 要求 | 现在如何保证 |
|---|---|
| batch 是从 0 开始的连续 prefix | `_committed_plan` 要求从新到旧的 index 为 `count-1, count-2, …, 0`（最后一个必须是 0）；否则 `ordered=False`，由 `_check_plan` 在原来的判定位置报 "not a contiguous prefix committed in order" |
| 每个 batch 只提交一次 | 按序遍历时，已见 index 恰为区间 `[previous, newest]`，落入此区间就是重复，报 "…but more than one snapshot committing it"，判定位置与原来相同（walk 之后、`_recover` 之前） |
| 单一计划，且 `unit_rows / chunk` 合法 | 同原逻辑：混入其他计划时报 "more than one plan"（walk 完成后抛出，malformed id 仍在 walk 中立即抛出）；非法计划时报 "no lawful plan" |
| 批次数与 `unit_rows / chunk` 一致 | `_check_plan`：`plan.unit_rows == len(positions)`，且 `(count-1)*chunk < unit_rows`；读取端 `_require_complete`：`count == ceil(N/chunk)` |
| 每个被消费的 batch 核对原 snapshot | `_survey_unit` 和 `_verify_batches` 对每个被证明的 batch 调用 `check_batch_snapshot`（fingerprint、`added_rows`），传入的是流式定位到的原 snapshot |
| 每个被跳过的 batch 核对原 snapshot | 证明阶段已核对全部已提交 batch；`_replayed_commits` 要求 snapshot 序列摘要相等，并逐个核对 `added_rows == 窗口行数` |
| 已提交行恰为这些 batch 的行 | 不变（`_same_numbers(seqs, base + positions[:count*chunk])`） |
| Raw position 顺序、REST 缺项判定 | 未改动 `_positions / _check_positions / _check_rest_unit / check_rest_page`；输出 ids 和 rows 按 batch 反转后仍为 position 升序 |
| 错误 fail closed | 所有新增分支都抛出 `CatalogIntegrityError`；流式重走发现与证明阶段不一致也会拒绝 |

## 3. 行为差异（全部是更严格或只改变报错文字，没有放宽）

1. **乱序但完整的 prefix 现在会被拒绝**。例如先提交 batch 1、后提交 batch 0：原代码只检查 index 集合，因此接受；新代码报
   "not a contiguous prefix committed in order"。唯一写入方 `_write` 在带 expected parent 的线性提交中按升序补写，
   所以合法 writer 不会产生这种历史。这与参考分支的 `ordered_batches` 语义一致。`rules.NORMALIZER_SPEC` 的文字
   （"committed indexes are a contiguous prefix"）和哈希**没有修改**；是否要在规格文字中明确 "in order"，留给 Codex 决定。
2. 重复提交的报错从 "…but N snapshots committing it" 改为 "…but more than one snapshot committing it"，因为不再计数。
   已检查的 canonical 测试没有匹配旧文字（`tests/infrastructure/revision/*` 中的 "2 snapshots committing it" 属于其他模块）。
3. 同一历史若**既乱序又重复**，O(1) 判定在第一次违规后停止分类，可能报 "contiguous prefix" 而不是 "more than one snapshot"。
   两者都是 fail closed。
4. 多个 batch 同时损坏时，证明循环改为从新到旧，因此先报告 index 最大的损坏 batch。单个损坏时报错不变。

## 4. 未解决与风险（不声称）

- **不解决 32 MiB 问题，也没有测量任何数字。** 调查记录显示剩余主要来源是 PyIceberg `TableMetadata.snapshots` 全量列表；
  本批不触及它。本批只去掉了 normalizer 自身随 batch 数增长的 `SnapshotInfo` 映射和 `done` 字典。
- **仍随 N 或批次数增长的部分（本批范围外）**：API 返回的 `CanonicalUnitNormalized.revision_ids`（O(N)）和 `commits`
  （O(N/M) 个 `BatchCommit`）；`_Survey.committed_ids`（服务于上述返回值）；`positions` 元组（O(N)）；`_same_numbers` 与
  `_close` 的全量 `arrival_seq` 数组；`verify_unit` 全量路径的 `committed_rows`。改动这些需要改变返回 API 或跨模块读取接口，
  超出本任务范围。
- **时间代价**：replay 或 resume 现在最多遍历 pinned history 三次：计划遍历 1 次（完整），证明流 1 次和 replay 流 1 次
  （都在 index 0 后停止）；原来只完整遍历 1 次。在 PyIceberg adapter 上，每次遍历是 `O(L·H)` 次比较（见
  `e1-single-load-history-implementation.md` §6），总比较量约增加到 3 倍。未计时。
- **未测试**：§2 和 §3 只来自代码审查。验收前需要补充定向测试：乱序 prefix 被拒；重复 batch 被拒；`_replayed_commits` 摘要
  不一致被拒；frozen view 下 `arrival_seqs` 跨 cache 淘汰边界；resume 后 `commits` 顺序与 `replayed` 标志；并重跑
  canonical、PIT、quality 与 PostgreSQL normalizer 测试，以及 E1-CAP-1 容量探针。

## 5. 静态检查（原样）

```text
$ uv run --offline ruff check .
All checks passed!
exit=0
$ uv run --offline ruff format --check .
788 files already formatted
exit=0
$ uv run --offline mypy
Success: no issues found in 604 source files
exit=0
$ git diff --check
exit=0
```

（第一次运行 `uv run` 时，在本 worktree 内创建了被 Git 忽略的 `.venv`，并从本机缓存离线安装 50 个包，没有修改系统环境。）

**未运行**：pytest（单元、PostgreSQL）、容量探针。

## 2026-09-28 后续开发状态

后续 E1-API 审计发现完整 `revision_ids` tuple 的结构占用与 32 MiB 门槛冲突；ADR-0076 已接受。当前开发分支将默认结果改为固定大小计数 / replay 摘要，并由 `iter_revision_ids()` 显式重证、按 Raw position 有序流式返回。此变化不改 `rules.NORMALIZER_SPEC`、revision identity 或持久化 batch 格式。容量 / DNET 工具与 Canonical 测试已改读摘要或显式 ID stream；本历史记录中“revision IDs / commits tuple 仍存在”描述的是本记录产生时的旧基线，不再是当前工作树状态。测试与容量 probe 均未运行。
