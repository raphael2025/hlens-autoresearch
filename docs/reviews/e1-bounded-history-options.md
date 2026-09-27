# E1-CAP-1：有界历史读取方案调查

| 字段 | 值 |
|---|---|
| 状态 | **DESIGN / NOT IMPLEMENTED / NOT VALIDATED** |
| 日期 | 2026-09-27 |
| 代码基线 | `05db662`（`codex/module-completion-coordination-2026-09-27` 头，含 `c7bbcfe` / `8261f32` 单次加载历史遍历与 `6ab7f5b` 计数化 plan） |
| 依赖版本 | `pyproject.toml:16` `pyiceberg[pyarrow,pyiceberg-core,sql-postgres]>=0.12.0`；`uv.lock:533-535` 锁定 **pyiceberg 0.12.0**；`uv.lock:574-575` 锁定 **pyiceberg-core 0.10.1** |
| 范围 | 只读调查：500k `resume` / `replay` 的 RSS 增长中哪些随 snapshot 历史 `H` 增长，以及有界内存方案。没有写生产代码，没有新增或运行测试 / 探针 |
| 与 E1-CAP-1 的关系 | 不关闭、不放宽 E1-CAP-1；32 MiB 门槛与 `2026-09-27-e1-review.md` 的失败结论不变 |

## 0. 证据边界（先读）

1. **依赖源码已核对。** Claude Code 按只读任务检查本机、与 `uv.lock` 锁定版本一致的 PyIceberg 0.12.0 源码（位于
   `.claude/worktrees/e1-bounded-history-options/.venv/lib/python3.13/site-packages/pyiceberg/`）；新增 §6 列出路径与行号。
   **[源码事实]** 来自直接读取的实现；**[推断]** 表示尚未测量或无法从源码单独证明。未修改文件、运行测试 / 探针或安装依赖。
2. **59.9 / 63.9 MiB 不是本文测得的。** 数值来自协调者（`2026-09-27-e1-resume-replay-memory-investigation.md`
   转述）。基线 `05db662` 的 `infrastructure/tools/capacity_probe.py` 没有 resume / replay 阶段，它的 `_measure`
   （`capacity_probe.py:209-226`）用的是 `ru_maxrss` 差值和 `tracemalloc` 峰值；`_normalize`（`:413-422`）使用
   默认 `microbatch_rows=25_000`（`normalizer.py:99`），而不是调查记录中的 M=256。所以本文不知道那两个数字的
   精确测量口径（基线、采样方式、结果对象是否计入）。
3. 下文所有 MiB 数量级都是**按对象布局估算的**，未经测量，只用来判断方向。

## 1. resume / replay 的调用链与 metadata 加载

resume / replay 都是对已有已提交 plan 的单元再调用一次 `CanonicalNormalizer.normalize_unit`（`normalizer.py:293-350`）。
按执行顺序：

| 步骤 | 位置 | 与 `H` 的关系 |
|---|---|---|
| 固定 head：3 张表各读两次 `_head` | `normalizer.py:497-520`、`:1157-1161` → `PyIcebergCatalogAdapter.load_table`（`iceberg_adapter.py:253-260`） | 每次 `_load` 都执行一次 `catalog.load_table`（`:635-639`），得到完整的 `TableMetadata` |
| 通过 view 读 head | `PinnedCatalogView.load_table`（`pit/view.py:50-56`）= `load_table` + `get_snapshot` | 两次完整加载；`get_snapshot` 在已加载列表中做 `snapshot_by_id`（`iceberg_adapter.py:307`） |
| Raw 位置 | `_positions`（`normalizer.py:619-634`） | **O(L)** 个 Python int，另有一份排序后的副本 |
| 证明窗口 | `_survey_unit` → `_raw_window` / `_prove`（`:551-555`、`:706-715`）→ `PersistedRowVerifier.verify_archive_elements`（`row_integrity.py:1490-1516`） | 每次 `scan_columns` 都经 `_require`（`iceberg_adapter.py:452`）完整加载；`_verified_archives` 经 `snapshots_of_batches` → `_history` 完整遍历 archive 表（`row_integrity.py:1569`、`:289-308`） |
| Raw 行批次索引 | `_verify_archive_row_batches` → `_indexed`（`row_integrity.py:1772-1790`、`:1108-1125`） | `_indexed_batches`（`:311-335`）为该 archive 的**全部** Raw 行批次保存 `SnapshotInfo`：**O(B_raw)**，按 head 缓存 8 份（`:222`），每次调用还会复制一份 |
| 已提交 plan | `_committed_plan` → `_unit_batches` → `history_from(self._adapter, …)`（`normalizer.py:729-807`、`row_integrity.py:351-368`）→ `PyIcebergCatalogAdapter.history`（`iceberg_adapter.py:315-378`） | 加载一次，在生成器存活期间持有 `iceberg.metadata.snapshots`（`:342`） |
| 已提交行时间 | `_committed_times`（`normalizer.py:844-858`） | 3 个 **O(L)** Arrow 列 |
| 逐批证明 | `_survey_unit` 中的 `_plan_snapshots` 循环（`normalizer.py:583-600`、`:809-842`） | 遍历生成器**全程**持有第 1 份 metadata；循环体里每次 `_raw_window`、`_check_committed_window`、`_check_unique` 的 scan 都各自再完整加载一次（第 2 份，临时）并规划扫描 |
| 修订 ID | `ids.append(...)`（`:598`）→ `committed_ids`（`:613`）→ `_write` 中 `ids = list(...)`（`:969`）→ 结果 `revision_ids`（`:345`） | **O(L)** 字符串 |
| 重放提交 | `_replayed_commits`（`normalizer.py:1005-1047`） | 第三次完整遍历历史；`found` 列表保存 **O(B_unit)** 个 `BatchCommit` |
| resume 的新提交 | `commit_batch`（`iceberg_adapter.py:380-413`）：`_require` + `_replay` 用 `ancestors_of` 走完整条祖先链（`:751-773`）+ `iceberg.append`（`:402`） | 每次提交都是 O(H) 时间；写入会产生一份新的完整 metadata（见 §2 H3） |
| 收尾 | `_close`（`normalizer.py:1101-1129`） | 两次 O(L) scan，外加长度为 L 的 `expected` 列表（`:1124`） |

**时间复杂度（当前实现）：** `history` 每一步做 3 次线性 `_listed_position`（`iceberg_adapter.py:349/351/357`），
而 `_unit_batches` 必须从 head 走完**整张表**的历史（重复和越序检查不允许提前停止），所以一次遍历是
**O(H_table²)** 次比较。一次 normalize 至少做 3 次这样的遍历，加上每次提交一次 O(H) 的 `_replay`。
H≈2k 时还能接受；随表的总历史增长（见 §2 末尾）会变成主要成本。

## 2. 内存项清单：哪些随 H 增长

记号：`L` = 单元行数；`M` = Canonical microbatch；`B_unit = ⌈L/M⌉`；`H` = 表的 snapshot 总数（**整张表**的
历史，不只是这个单元）；`B_raw` = 该 archive 的 Raw 行批次数（D2 默认 25 000 行一批，`revision/store.py:140`）。

### 随 H 增长

| # | 内存项 | 证据 | 备注 |
|---|---|---|---|
| H1 | 每次加载得到的 `TableMetadata.snapshots` 及每个 snapshot 的 summary | 仓库侧：`iceberg_adapter.py:342` 直接对内存中的 `metadata.snapshots` 做下标访问，`:307` `snapshot_by_id`，`:757` `ancestors_of(…, iceberg.metadata)`，访问时都没有 IO，所以加载后整份列表已在内存中 | 遍历期间**至少同时驻留两份**：`_plan_snapshots` / `_replayed_commits` 生成器持有一份，循环体内每次 scan 的 `_require` 又加载一份。另外是否同时保留原始 JSON 字节、`snapshot-log`、`metadata-log`：**【依赖未核实】**`pyiceberg/serializers.py`（`FromInputFile.table_metadata`）、`pyiceberg/table/metadata.py`（`TableMetadataCommonFields`）、`pyiceberg/catalog/sql.py`（`SqlCatalog.load_table`） |
| H2 | 扫描规划时的 manifest list / manifest 条目 | 仓库侧：表属性只有绑定项和 `commit.retry.num-retries=0`（`catalog/definitions.py:241-250`），**没有**启用 manifest merge；每个 batch 是一次 `iceberg.append`（`iceberg_adapter.py:402`） | 若 PyIceberg 0.12 在未开启 merge 时使用 fast append（每次提交新增一个 manifest，并保留全部旧 manifest），则 head 的 manifest list 约有 H 项，每次 pinned scan（`:466-472`）都要读取并评估它们：O(H) 临时对象、O(H) 次文件打开。**【依赖未核实】**`pyiceberg/table/__init__.py`（`Transaction.append` / `update_snapshot().fast_append`）、`pyiceberg/table/update/snapshot.py`（`_FastAppendFiles` / `_MergeAppendFiles`）、`DataScan.plan_files` 是否整批物化 |
| H3 | 提交路径（只有 resume 会提交） | 仓库侧：adapter 依靠 SQL catalog 对 metadata 指针做 CAS（`iceberg_adapter.py:11-13`）；按 Iceberg 规范，每次提交都要写出一份新的完整 metadata 文件 | 每次提交时，旧 metadata、新 metadata 对象和序列化后的 JSON 同时存在，都是 O(H)。**【依赖未核实】**`SqlCatalog.commit_table`、`update_table_metadata`、`ToOutputFile.table_metadata` |
| H4 | `_replayed_commits` 的 `found` 列表与结果中的 `commits` 元组 | `normalizer.py:1022-1047`、`:346` | O(B_unit) 个小 dataclass（2k 项约 0.x MiB，估算）。这是**本仓库自己**建立的、随批次数增长的 Python 对象 |
| H5 | `_indexed` 的缓存与每次调用的复制 | `row_integrity.py:1108-1125`、`:311-335` | O(B_raw) 个 `SnapshotInfo`。探针默认值下 B_raw≈20，影响很小；Raw microbatch 变小时它会随 Raw 表历史增长 |

### 不随 H，而随 L 增长

| 内存项 | 位置 | 数量级（估算，500k） |
|---|---|---|
| 修订 ID 字符串：`"crev1-"` + 64 位十六进制（`canonical/rules.py:385-400`），约 70 个 ASCII 字符 | `normalizer.py:598` → `:613` → `:969` → `:345` | 每个 CPython 对象约 110–120 B → 仅字符串就约 **55 MiB**，另有 3～4 个 O(L) 指针数组，每个约 4 MiB |
| Raw 位置 int | `:634`（`to_pylist` 加 `sorted` 副本），`_Survey.positions` | 约 19 MiB，排序时临时翻倍 |
| `_committed_times` 三列 Arrow 数组 | `:849-858` | 约 12 MiB |
| `[base + position …]` 列表 | `:601`、`:473`、`:1124` | 基数 ≥ 2³²，因此每个都是新的 int：临时约 19 MiB |

### 归因结论

- M 固定时 `B_unit = ⌈L/M⌉`，从 10k 到 500k，**L 和单元的 H 同时放大 50 倍**。现有两点测量无法区分
  这部分增长来自 H 还是 L。
- 按上面的估算，**只算 O(L) 的修订 ID 输出，数量级就已与整段 59.9 / 63.9 MiB 相当**；H1 在 H≈1954 时，
  每份大约是个位数到十几 MiB（**未测，取决于【依赖未核实】的解析对象大小**）。因此“59.9 / 63.9 MiB
  主要来自 Catalog snapshot 列表”的归因**尚未成立**；即使历史读取完全有界，500k 仍可能超过 32 MiB。
- 另外，H 是**整张表**的历史，会随所有单元、所有日期的提交持续增长，与当前单元大小无关。在生产中，
  它才是长期问题：每一次加载、scan 和提交都会随之变大（H1–H3），当前遍历的时间也是 O(H²)。

## 3. 可实现方案（最多 3 个）

共同前提（仓库侧已证实）：pin、`_verified`、每次 `scan_columns` 和每次提交都经过 `_require` →
`catalog.load_table`（`iceberg_adapter.py:641-645`、`:452`、`:384`）。只要 scan 和提交仍使用 PyIceberg 的
`Table`，**每一次**这样的调用都会临时物化一份 O(H) metadata。因此，只改历史遍历的方案最多只能去掉“遍历
期间长期持有的那一份”，无法让进程峰值与 H 无关。

### 方案 A：每次调用从同一 metadata 文件流式建立磁盘历史索引（只让历史遍历有界）

- **做法：** 对 `_verified` 已核对过的**同一个** `metadata_location`，用流式 JSON 解析读取 `snapshots` 数组，
  逐条把（列表位置、snapshot_id、parent_id、timestamp_ms，以及 `_snapshot_info` 需要的 summary 字段）写入
  调用级临时目录中的 stdlib `sqlite3` 文件，并以 `(snapshot_id, position)` 建索引。`history` 按 id 取
  `MIN(position)`（保留 first-match 语义），在位置上沿用 Floyd 双指针，生成器关闭时删除临时目录。
  `SnapshotHistory` 协议和 `PinnedCatalogView.history` 的签名保持不变。
- **额外内存：** 遍历部分对 H 为 O(1)：解析缓冲区只取决于最大单个 token，sqlite 页缓存由 `cache_size`
  限定。**进程峰值仍是 O(H)**，因为 pin / scan / 提交还会临时加载完整 metadata（H1–H3），并且
  `_verified` 需要从同一文件读出 properties / schema / spec；如果继续用 PyIceberg 读取这些内容，又会发生
  一次完整加载。对 L 不变。
- **时间：** 建索引 O(H log H)，每步 O(log H)。整次遍历 O(H log H)，优于当前的 O(H²)。
- **依赖 / ADR / 契约：** stdlib `json` 不能流式解析。要么引入 `ijson` 这类新依赖（03-data 冻结了依赖清单，
  且需要 H12 安装授权，因此要写 ADR），要么手写增量 JSON 词法器（不增加依赖，但风险高）。不改变
  `core/contracts`。临时索引是按调用派生、用完即删的缓存，不参与崩溃恢复，**可能**不属于 ADR-0028:218
  所禁止的“journal 或可变 sidecar”，但这个判断需要 Codex 确认。
- **语义：** 固定 pinned head：索引只来自一个 metadata 版本，与现有“一次遍历只读一个版本”的语义等价。
  父链、悬空 parent、自指 parent、多节点环、重复 id（first-match）都可以按位置逐项复刻。
  **风险点在 malformed：** PyIceberg 在解析时会用 pydantic 拒绝一些非法内容（例如字段类型错误、未知
  operation）。自写解析器必须拒绝**完全相同的集合**，否则 PyIceberg 会拒绝的文件可能被索引接受，形成
  fail-open。【依赖未核实】`pyiceberg/table/snapshots.py`（`Snapshot` / `Summary` 的校验器）。
- **重启恢复：** 不变，因为恢复仍只依赖 Iceberg 已提交状态，索引每次调用都重建。
- **结论：** 这是只能部分改善的方案：去掉长期持有的那一份、把时间复杂度从 O(H²) 降下来，但**不能**
  让 resume / replay 的峰值与 H 无关，也不影响 O(L) 项。

### 方案 B：让 H 本身有界（批次账本移出 snapshot 历史 + snapshot 过期 + manifest merge）

- **做法：** Canonical 表的幂等、plan 与“提交一次、按顺序提交”的证明改为只来自**已提交行**：
  每行已带 `lineage_source_revision_id` 和 `arrival_seq`，规范行也可以从 Raw 重算，指纹规则可以对行重算；
  重复提交表现为重复的 `revision_id`，可由 `_check_unique` / `_close` 类检查捕获。然后定期执行 snapshot
  过期，把 H 限制在保留窗口内，并在表定义中启用 manifest merge，限制 manifest 数量。
- **额外内存：** H 有上界 R（保留窗口），所以 H1–H3 都是 O(R)，与累计历史无关。仍有 O(L) 项。
- **时间：** 每次操作 O(R)。过期和 merge 是额外的维护作业。
- **依赖 / ADR / 契约：** **需要改冻结契约并写 ADR**：
  - `core/contracts/catalog.py:27-39` 规定幂等是“按 `(table, batch_id)` 查已有提交”，而且“重建的 adapter
    对同一 `batch_id` 重放得到同一 snapshot”；
  - ADR-0028:218 规定恢复只依赖已提交行与 Raw；
  - 新增表属性会导致定义版本升级（`definitions.py:241-250` 会在每次加载时校验属性）；
  - 03-data 冻结了表定义。
  需要 Raphael 批准（H1 规则）。
- **语义：** 固定 pinned head **会受影响**。旧的数据集 manifest 通过 `snapshot_bindings` 绑定旧 snapshot；
  过期之后，time travel 会得到 `SnapshotNotFound`；即使用 tag 保留它们，祖先链也会断开，`history_from`
  会对悬空 parent 抛 `SnapshotNotFound`。现有 fail-closed 规则会因此拒绝读取旧数据集，影响实验可复现
  （ADR-0009 与 06-experiment 的复现元组）。父链和重复证明要从 snapshot 迁到行上，也就是改变证明对象。
  【依赖未核实】PyIceberg 0.12 是否提供 expire-snapshots API，以及 ref 保留规则。
- **重启恢复：** 语义改变：`already_committed` 不再能返回“同一 snapshot”，只能返回“同样的行”。
- **风险：** 最大。它同时触及 Catalog 契约、ADR-0028、03-data 和可复现性，远超出 E1 的范围。

### 方案 C：读路径完全绕过 PyIceberg 的 metadata 模型（自有流式 metadata / manifest / Parquet 读取器）

- **做法：** 在方案 A 的基础上，pinned scan 也不再构造 `Table`：流式读取 manifest list 和 manifest
  （Avro），按分区 / 指标剪枝后，用 pyarrow 直接读取数据文件。`_verified` 也由流式解析得到的
  properties / schema / spec 完成。
- **额外内存：** replay（没有提交）时读路径对 H 为 O(1)，只剩流式缓冲区。resume 的提交**仍然**走
  PyIceberg，因此仍是 O(H)（H3）；要让它也有界，就必须自写 Iceberg 提交器，这等于替换核心技术。
- **时间：** 每次 scan O(H) 流式 IO，与当前相当，但没有 O(H) 常驻对象。
- **依赖 / ADR / 契约：** 需要 ADR（ADR-0021 规定经由 PyIceberg SQL Catalog adapter；自写读取器相当于
  第二个 Iceberg 实现）。可能还要引入 `ijson`，Avro 流式读取是否可复用 PyIceberg 的 reader 属于
  【依赖未核实】（`pyiceberg/avro/file.py` 是否逐条迭代）。
- **语义：** pinned head、父链、重复 / malformed 都必须与 PyIceberg 逐项等价，包括 schema 演进、分区
  演进（C3 `evolve_partition_spec`）、删除文件和统计信息。任何不一致都可能读错行，而且在
  PIT / 数据集层是静默的 fail-open 风险。
- **重启恢复：** 不变，因为只读。
- **风险：** 很高。它等于维护一套自己的 Iceberg 读取实现，而且仍然不能解决 resume 的提交路径。

### 不在上述三项内的“降常数”做法（不能有界）

分段遍历：固定同一个 `metadata_location`，每段取 K 个小元组，然后释放 metadata，下一段重新加载，并要求
`metadata_location` 与第一段一致，否则 fail closed，同时让 Floyd 状态跨段保留。这能把“同时驻留 2 份”降为
“同一时刻最多 1 份”，但每次 scan 仍会临时加载 1 份，所以峰值仍是 O(H)，只是常数变小；代价是额外
O(B_unit / K) 次完整加载。不需要 ADR，也不改变语义，但**不满足**有界要求。

## 4. 结论

**当前源码调查没有找到既能让 resume / replay 峰值与 H 无关、又能精确保留语义的已验证实现方案。** 这是对已调查路径的结论，不是对所有可能实现的证明：

- Catalog 契约规定幂等重放按 `batch_id` 从已提交 snapshot 恢复，ADR-0028 规定恢复只依赖 Iceberg 已提交
  状态且不用 sidecar；ADR-0021 规定经由 PyIceberg adapter 读写；在这些前提下，每一次 load / scan / 提交都
  会临时物化 O(H) metadata。上面第一条是仓库侧事实，第二条（scan / 提交内部）部分属于【依赖未核实】。
- 方案 A 在契约内可行，但只能让“历史遍历”有界；方案 B 能让 H 有界，但要改契约并影响固定 head 与可复现性；
  方案 C 只能让 replay 读路径有界，而且要替换核心技术。
- 另外，按估算，O(L) 的修订 ID 输出与位置列表本身就可能超过 32 MiB（§2），这与 H 无关。

```
SOURCE INVESTIGATION FINDING (no architecture decision made)
- 冲突/问题：E1-CAP-1 要求“不随 N 或批次数增长”，但在当前 Catalog 契约 + ADR-0028 + ADR-0021(PyIceberg 0.12)
  下，每次 catalog 调用都会临时物化 O(H_table) 的 metadata，且 H 是整表累计历史；只改历史遍历无法让进程峰值
  与 H 无关。
- 涉及文档/契约：core/contracts/catalog.py（commit_batch 幂等与重放语义）、ADR-0021、ADR-0028（恢复不用 sidecar）、
  docs/architecture/03-data.md（表定义与依赖冻结）、docs/reviews/2026-09-27-e1-review.md（E1-CAP-1 验收口径）。
- 候选方向：见下方历史 Decision Packet；A / B / C 均未获批准。先对被调查路径做 L / H 分离容量测量，再决定是否存在不改变冻结契约的可行实现。
- 当前影响：静态源码调查不是容量测量，不能判定所有候选实现均不可行；E1-CAP-1 继续阻断，直到完整容量证据满足既有关闭标准。
```

## 历史 Decision Packet（选项未批准）

ID: D-E1-HIST
QUESTION: E1-CAP-1 的“有界内存”是否把 PyIceberg 表 metadata 随整表 snapshot 历史 H 的 O(H) 临时驻留算在 normalizer 的工作集内？
WHY_IT_MATTERS: 如果算在内，那么在现有契约下没有能关闭它的实现，必须改契约（方案 B）或替换核心读写技术（方案 C）；如果不算在内，E1-CAP-1 可以只针对 normalizer 自身的 O(L) / O(B) 状态来判定，H 则作为单独的、带明确预算的平台容量项跟踪。
OPTIONS:
A. 不算入：E1-CAP-1 只约束本仓库建立的状态（O(L) 输出、O(B) 列表、缓存）；PyIceberg metadata 另设 H 上限与监控（例如按表记录 snapshot 数、限定 microbatch 下限），不改契约。方案 A（磁盘历史索引）可作为降低 O(H²) 时间和一份常驻副本的后续批次。
B. 算入，并接受契约变更：批次账本移出 snapshot 历史 + snapshot 过期 + manifest merge（方案 B）；需要新 ADR、改 Catalog 契约和 ADR-0028，并重新定义旧数据集 pinned head 的可复现性。
C. 算入，但不改契约：自写 Iceberg 流式读取器（方案 C）；需要 ADR 替换核心技术，而且 resume 的提交路径仍是 O(H)。
RECOMMENDATION AT DRAFT TIME: A（现已由 §6 取代）。当时判断 B / C 影响范围过大且 O(L) 项可能独自超过门槛。
IMPACT AT DRAFT TIME: A 会修订 E1-CAP-1 验收口径；§6 已决定不改口径。B / C 各自需要 ADR。
BLOCKS: E1-CAP-1 的任何修复批次，以及 Phase 1 验收。
DEFAULT_IF_UNDECIDED AT DRAFT TIME: E1-CAP-1 保持阻断。§6 已正式重申完整工作集与 32 MiB 门槛；A / B / C 均未采纳。

## 5. FOLLOW-UP（未执行，需要批准）

1. **L / H 分离测量**（只描述，未运行）：在隔离子进程中用相同 L、不同 M（256 与 25 000）测 resume / replay，
   分离 H；再用相同 M、不同 L 测一次。测量时用 `tracemalloc` 快照按 traceback 文件聚合
   （`pyiceberg/*` 与 `infrastructure/canonical/*`、`infrastructure/revision/*` 分开统计），并记录结果对象
   （`revision_ids`）是否计入。
2. 尚未核实的依赖点限于 PyIceberg / Avro 的 malformed JSON 等价拒绝集合、Avro manifest reader 的流式细节、
   stdlib `Executor.map` 队列峰值，以及 refs / snapshot expiry 对历史 pinned heads 的精确行为；这些都须源码核对或实验验证，
   不得把推断当作容量测量。
3. O(L) `CanonicalUnitNormalized.revision_ids` 和位置 / 时间列仍是独立增长源；它们也在 FULL_PROCESS_WORKSET 门槛内，
   不能因不属于 PyIceberg metadata 而排除。若要改变返回接口或严格证明语义，须先核对冻结契约并另立 Proposed ADR。

## 6. E1-CAP-1 既有容量口径确认（2026-09-27）

Codex 重申既有 E1-CAP-1 关闭标准：32 MiB 峰值增量统计包含完整进程工作集，涵盖 PyIceberg metadata、Parser / scan 临时对象、Normalizer 内部状态，以及 API 返回结果对象。上方选项 A 不采纳；不改变已批准的 E1 验收条件，不把依赖内部对象从容量范围排除，也不把它改成独立的未来监控项。这是既有验收口径确认，不是架构决定或 ADR。

保持 32 MiB 上限、完整严格读取与逐项校验语义；若已调查的实现路径无法满足，就继续阻断 E1 并调查候选实现，必要时再提出明确的架构决策，不以修改口径关闭。选项 B / C 涉及冻结契约或核心技术替换，当前均未批准。

PyIceberg 源码事实仅用于完善实现调查，不会自动改变上述容量范围或 E1 关闭门槛。原 Decision Packet 是起草时的未决选项；A / B / C 均未获批准。

## 7. PyIceberg 0.12.0 本机源码复核（只读；未测容量）

路径前缀 `PI` = `.claude/worktrees/e1-bounded-history-options/.venv/lib/python3.13/site-packages/pyiceberg/`；版本由 `uv.lock:533-535` 锁定。Claude 只做源码读取，没有改文件或运行探针。

| 源码位置 | 已核实事实 | 对 E1 的含义 |
|---|---|---|
| `PI/serializers.py:90-95,113-116`；`PI/table/metadata.py:181,187,195,211` | metadata JSON 整体读取后交给 Pydantic `model_validate_json`；snapshots、snapshot_log、metadata_log 是普通 list，summary 含 dict。 | 每次 metadata load 的 snapshots history 确定是 O(H) 对象；实际字节数与 RSS 尚未测。 |
| `PI/catalog/sql.py:224-240,356-359` | SQL Catalog 行保存 metadata_location；load_table 解析完整元数据文件。 | repo 的每次 `_require` / adapter load 都会引入完整 history 对象。 |
| `PI/catalog/sql.py:503-554`；`PI/table/update/__init__.py:461-472,502-507,683-720`；`PI/serializers.py:134` | commit 重新 load 完整 metadata，构造新 snapshot 与日志列表、深拷贝 / validate，并序列化完整新文档后 CAS 更新 metadata_location。 | 提交峰值同时保留多少副本仍未知，但不是固定于当前 microbatch 的简单写入成本。 |
| `PI/table/__init__.py:259-261`；`PI/table/update/snapshot.py:281,309,319,332,343,376,696` | transaction metadata getter 重算更新并复制完整 metadata，append producer 多次访问。 | 多份 metadata 副本导致峰值升高属合理推断，须实测，不虚报副本数量。 |
| `PI/table/__init__.py:428-434,1723-1724,207-208`；`PI/table/update/snapshot.py:229-275,317-339,687-705` | 默认关闭 manifest merge；普通 append 使用 fast append，保留旧有效 manifest 并添加新 manifest / manifest list。 | 长期逐批 append 会令 manifest list 增长；scan 的匹配文件规划也有临时成本。 |
| `PI/table/__init__.py:2754-2791,2806-2838`；`PI/manifest.py:862-884,895-984` | 本地 planner 构造完整 `FileScanTask` list；manifest 内容加载为 list；进程内还有默认容量 128 的 ManifestFile LRU。 | 除 metadata 外，扫描对象与缓存也可能随文件量增长；具体 RSS 尚未测。 |
| `PI/table/__init__.py:2470-2509`；`PI/io/pyarrow.py:1792-1869` | `to_arrow()` 收集并合并完整结果；batch-reader 每个任务仍将文件结果批次列表化，delete files 预先读取。 | repo `scan_columns()` 调用 `to_arrow()`，不是有界 reader；batch-reader 也未被证实有全局严格上界。 |
| `PI/table/metadata.py:239-241,322-326`；`PI/table/snapshots.py:420-434` | snapshot_by_id 线性查找，ancestry 遍历每一步再按 ID 搜索。 | `ancestors_of` 的 O(H²) 是时间复杂度推断；当前单次 history 优化只绕过 repo 特定重复加载，不改变 PyIceberg 本身的完整列表解析。 |
| `PI/table/update/snapshot.py:1236`；`PI/table/update/__init__.py:514-551,737-750` | 存在 ExpireSnapshots；过期时会清除被删除 snapshots，metadata-log 默认仅保留最近 100 项。 | 工具存在并不批准过期旧 pinned snapshots；历史绑定兼容与祖先语义仍须调查。 |

**容量结论：** PyIceberg 0.12 的 SQL load / commit / scan 路径都不能避免完整 snapshot metadata 的物化；所以在 FULL_PROCESS_WORKSET 下，现有单次 history walk 与 normalizer plan 计数优化只能降低仓库层重复状态或遍历时间，无法单独证明 E1-CAP-1 通过。fast-append 的 manifest 增长、PyIceberg metadata history 和本仓库 O(L) 输出 / positions 都是候选增长源。源码审查不是容量测量；不得据此给出 32 MiB PASS。

**未核实 / 未测：** 每个 Snapshot / summary 的真实占用；具体 append 的 metadata deep-copy 次数与峰值 RSS；`Executor.map` 的任务排队峰值（Python 标准库实现未独立核对）；Avro manifest reader 的分块边界；malformed JSON 的自定义流解析与 Pydantic 拒绝集合是否完全一致；过期快照后的 pinned ref / parent 具体语义；10k / 100k / 500k 隔离进程容量结果。
