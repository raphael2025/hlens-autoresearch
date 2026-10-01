# E1-CAP-1 主线探针记录：`main@5c3b516`（2026-10-01，部分完成）

## 结论

本次正式协议运行**未完成，不是 PASS 或 FAIL**：完成 10k / 100k 的 repeat 0 全部六个 stage 与 500k repeat 0 的 `verify_archive`、`write_crash` 后，因测量中发现的二次耗时问题（见下）而停止，改在修复后的主线上重跑。停止过程中操作者误删了运行中的 500k 工作目录，导致 `resume` 子进程失败、JSON 记为 `status=error`；已完成 stage 的数值不受影响，但该 JSON 不构成 E1-CAP-1 证据（`e1_cap1_evidence=false`）。

## 已完成 stage（RSS 增量 MiB / stage 秒）

| stage | 10k | 100k | 500k |
|---|---|---|---|
| verify_archive | 41.6 / 4.5 | 44.1 / 78.8 | 45.0 / 1054.9 |
| write_crash | 48.2 / 6.1 | 59.6 / 139.5 | 60.3 / 2739.7 |
| resume | 46.5 / 9.5 | 62.2 / 323.0 | — |
| replay | 43.0 / 9.3 | 50.0 / 420.1 | — |
| read_batch | 32.3 / 0.5 | 28.6 / 2.9 | — |
| metadata | 1.7 / 0.3 | 3.8 / 0.3 | — |

判定规则（探针内定义）：每个 stage 的 `growth = max(delta) − min(delta)`，跨所有 N 与 repeat，须 ≤ 32 MiB；固定的进程级开销不计入。已完成部分中，`verify_archive` 增长 3.4、`write_crash` 12.1，均在门槛内；`resume` / `replay` 在 500k 未测。对比 `b5f80fe` 的中断记录，100k `verify_archive` 由 366 秒降至 78.8 秒（E1-ARCHIVE-REUSE 生效）。

## 发现：调查路径的二次耗时

profile（40k replay，198 秒）显示 160 秒耗在证明调查对每个已提交窗口的两次 Canonical 目录扫描（`_check_committed_window`、`_check_unique`）。按 ADR-0075，每次扫描都 preflight 并规划快照的全部 manifest，而每个 microbatch 提交新增一个 manifest，故总成本 O(窗口数 × manifest 数)。tracemalloc 显示 Python 堆峰值约 20 MiB，未见随窗口累积的保留对象；Iceberg `TableMetadata` 每快照约 5–6 KB，500k 时单份约 10 MiB。

处理：新增并收口 E1-CANONICAL-WINDOW-REUSE（PR #21，`main@50d6bb6`）：第二窗口起每单元一次 block spool + 一次 `(revision_id, time)` 索引。40k replay 198 → 48 秒；5k/20k/40k 诊断矩阵 RSS 增量不变或略降（诊断，非证据）。

仍开放：写入后的逐提交 read-back 在新快照上同样遍历全部 manifest，`write_crash` 时间仍超线性（500k 约 46 分钟）。若要改为按快照增量读取，会改变 ADR-0075 preflight 的范围，须先立 ADR；当前不影响 RSS 判定。

## 原始数据

- [已完成 stage（JSON lines）](artifacts/e1-cap1-main-5c3b516-partial-20261001-stages.jsonl)
- [探针 JSON（status=error）](artifacts/e1-cap1-main-5c3b516-partial-20261001.json)
- [RSS samples](artifacts/e1-cap1-main-5c3b516-partial-20261001-samples.jsonl)
