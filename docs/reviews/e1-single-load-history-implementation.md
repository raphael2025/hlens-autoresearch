# E1 单次 metadata load 的快照历史遍历（实现记录）

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-27 |
| 分支 | `codex/e1-single-load-history-2026-09-27`（基线 `88b2090`） |
| 范围 | `infrastructure/catalog/iceberg_adapter.py`、`infrastructure/revision/row_integrity.py`、`infrastructure/pit/view.py` |
| 状态 | **IMPLEMENTED / UNTESTED**：只跑了静态检查；按 Raphael 当前指示，未添加、未运行测试或容量探针 |
| 与 E1-CAP-1 的关系 | **不关闭 E1-CAP-1**；32 MiB 门槛不变，`docs/reviews/2026-09-27-e1-review.md` 的失败结论不变 |

## 1. 动机

`docs/reviews/2026-09-27-e1-resume-replay-memory-investigation.md` 指出，resume / replay 的证明路径要沿 Canonical 表的
snapshot 父链做历史遍历。在本基线上，`row_integrity.history_from()` 与 `_history()` 每走一步都调用一次
`adapter.get_snapshot()`；对 `PyIcebergCatalogAdapter` 来说，这意味着每一步都要 `load_table`、重新校验定义绑定，
再对 `metadata.snapshots` 做一次线性查找。历史长度为 `H` 时，这是 `H` 次 metadata 加载和 `O(H²)` 次比较。

本批只减少这类重复加载：一次遍历只加载、校验一次 metadata。

## 2. 改动

1. **`PyIcebergCatalogAdapter.history(table, snapshot_id) -> Iterator[SnapshotInfo]`**（infrastructure 专用，不进入 core Protocol）
   - 惰性生成器：第一次 `next()` 时 `_require` + `_verified` 一次（数据库故障仍在 `_backend("history")` 内映射成 `CatalogUnavailable`）；
   - 对同一个 `metadata.snapshots` 建立 `snapshot_id → 首次出现位置` 的 int 索引，然后沿 `parent_snapshot_id` 往下走，从新到旧输出。
2. **`row_integrity.SnapshotHistory`**：`runtime_checkable` Protocol，只声明上面的 `history` 方法，用来表达可选能力；`RevisionCatalog` 没有改。
3. **`row_integrity.history_from()`**：若 catalog 满足 `SnapshotHistory` 就走 `history`，否则保留原来的 `get_snapshot` 父链遍历。`snapshot_id is None` 时仍然什么都不输出，也不加载。
4. **`row_integrity._history()`**（当前 head 的遍历，供 `snapshots_of_batches` / `_indexed_batches` 使用）：先用 `load_table` 取当前 head（不变），再用 `history_from(parent)` 走祖先，因此也能用到单次加载。
5. **`PinnedCatalogView.history()`**（触及 `infrastructure/pit/view.py` 的原因）：F1 的 selector / 各 verifier 通过 view 调 `history_from`。view 本身没有 `history`，就只能走回退路径，这样底层 adapter 的单次加载在 pinned 读取时会失效。新方法只委托 `history_from(self._adapter, …)`。和 `view.get_snapshot` 一样，它读的是显式 snapshot 的不可变祖先，与 bindings 无关；底层不是 PyIceberg adapter 时仍走回退路径。

受益调用方（不修改代码）：`CanonicalNormalizer._committed_plan`、`canonical/listings.py`、`quality/reporter.py`，以及
`PersistedRowVerifier._indexed`。`revision/exchange_info_store._history` 自带 `get_snapshot` 遍历，本批没有改（见 §6）。

## 3. 语义保持（逐步与原 `get_snapshot` 遍历对照）

| 情形 | 原遍历 | 新 `history` |
|---|---|---|
| 顺序 | 起点在前，逐个父节点，newest-first | 相同 |
| 每项内容 | `get_snapshot` → `_snapshot_info` | 同一个 `_snapshot_info`，`SnapshotInfo` 相同 |
| 起点 id 非 str / 格式不符 / 不存在 | `SnapshotNotFound("table T has no snapshot 'X'")` | 同样的 regex、同样的异常与消息 |
| 悬空 parent | 下一步 `get_snapshot(parent)` → `SnapshotNotFound(parent)`，子节点已先输出 | 相同（先输出子节点，再对 parent 报错） |
| 重复 snapshot id | `snapshot_by_id` 返回列表中**第一个** | 索引用 `setdefault` 记录首次位置，结果相同 |
| 自指 parent（1 节点环） | `_snapshot_info` 构造失败 → `CatalogIntegrityError("… inconsistent metadata")` | 相同，还没到环检测就先失败 |
| 多节点环 | **永不终止**（本基线没有环检测） | 已输出的数量达到不同 id 数时，再走一步抛 `CatalogIntegrityError("table T has a cycle in snapshot history")` |
| summary 不一致 | `CatalogIntegrityError` | 相同（同一个 `_snapshot_info`） |
| 表不存在 / 定义漂移 | 起点加载时抛 `TableNotFound` / `CatalogIntegrityError` | 相同 |
| `snapshot_id=None` | 空 | 空（`history_from` 不调用 `history`） |

唯一的行为差异是多节点环：原路径不会终止，新路径 fail-closed。这没有放宽任何判定。另一处变化是，同一次遍历现在
始终读同一个 metadata 版本；原路径每步重新加载，遍历期间若有并发 snapshot 过期，可能看到混合版本。

## 4. 不解决的问题（明确不声称）

- PyIceberg 的 `TableMetadata.snapshots` 仍是**整张表全部 snapshot 的 Python 列表**（含 summary），一次加载就整份驻留，并在遍历期间一直被引用；新增的 id → 位置索引也随 `H` 线性增长（只含 int）。
- 原路径每步只临时持有一份 metadata，所以本改动**不会降低单次遍历的 metadata 驻留峰值**；它减少的是加载次数、解析 / 分配次数和查找时间。是否能降低 RSS（例如分配器碎片）**未测量**，不作任何推断。
- 因此 E1-CAP-1（500k `resume` / `replay` 增长 59.9 / 63.9 MiB，门槛 32 MiB）**仍然阻断**。调查记录中的方向（对同一 metadata 版本建立磁盘背书的历史索引，或用有界解析器流式读取 metadata）仍待另行设计。
- 本批没有改 normalizer 自身的状态，例如 `_committed_plan` 的 `batches` 字典；也没有改门槛、Profile、core 冻结契约或 Phase 1 状态。

## 5. 静态检查（原样）

```text
$ uv run --offline ruff check .
All checks passed!
exit=0
$ uv run --offline ruff format --check .
787 files already formatted
exit=0
$ uv run --offline mypy
Success: no issues found in 604 source files
exit=0
$ git diff --check
exit=0
```

第一次运行 mypy 报过一次错（循环变量 `position` 与后面的 `int | None` 复用），把循环变量改名为 `index` 后得到上面的结果。原输出：

```text
infrastructure/catalog/iceberg_adapter.py:333: error: Incompatible types in assignment (expression has type "int | None", variable has type "int")  [assignment]
Found 1 error in 1 file (checked 604 source files)
```

另外做过一次导入检查（不是测试）：`issubclass(PyIcebergCatalogAdapter, SnapshotHistory)` 与
`issubclass(PinnedCatalogView, SnapshotHistory)` 均为 `True`，`pit.view` → `revision.row_integrity` 没有循环导入。

**未运行**：pytest（单元、PostgreSQL）、容量探针。

## 6. 风险与后续（未执行）

- **未测试**：§3 的逐项等价只来自代码审查。验收前需补充定向测试：新旧路径逐项相等、悬空 parent、重复 id、自指、多节点环、`None` 起点、view 委托，并重跑 canonical / pit / quality / revision 的现有测试与 PostgreSQL normalizer 测试。
- **代理绕过**：`SnapshotHistory` 按属性存在判断。如果某个 `__getattr__` 透明代理只覆写 `get_snapshot`（做注入或记录），却经 `__getattr__` 暴露内部 adapter 的 `history`，`history_from` 会绕过它的 `get_snapshot`。本仓库已知的 `__getattr__` 代理（`test_store_*` 中的 `_CrashAfter` / `_RaceOnce` / `_RecordingAdapter`）只覆写 `commit_batch`，不受影响；显式实现 `get_snapshot` 的 `ProxyCatalog` 与 fakes 没有 `history`，仍走回退路径。新增代理时需注意。
- **生成器生命周期**：新遍历在生成器存活期间持有整份 metadata；调用方若长期保存一个未耗尽的迭代器，驻留会延长。现有调用方或者在一个循环 / `list()` 中耗尽它，或者像 `quality/reporter.py` 的 `any(...)` 那样提前结束后立即丢弃它。
- **提前结束的调用方**：`any(...)` 在前几步命中时，原路径只加载一两次 metadata；新路径也只加载一次，但会先用 `O(H)` 建好 id 索引。错误仍只在走到出错的那一步时才抛出，语义不变，但这类调用的最好情况在时间上略差。
- **FOLLOW-UP**：`exchange_info_store._history` 仍按 `get_snapshot` 逐步遍历，可改为 `history_from`，本批未做；E1-CAP-1 的 metadata 全量列表问题需要单独设计（见 §4）。
