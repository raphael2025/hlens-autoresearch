# E1-CAP-1：阻断项与设计路径对账

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-28 |
| 基线 | 当前本地 `main`，起始提交 `2fc89f1` |
| 类型 | 源码审计、设计路径与分步实现记录；未运行 probe / 测试 |
| 状态 | **BLOCKED：容量条件与当前全量返回 API / Iceberg 历史模型存在未解决冲突** |
| 权威验收口径 | `docs/reviews/2026-09-27-e1-review.md`：完整进程工作集、32 MiB 增量上限；阈值与严格证明语义均不改 |

## 结论

E1-CAP-1 仍阻断。当前主线没有可复用的 E1-CAP-1 RSS 结果；旧候选 `fix/e1-cap1@a75278e` 的 59.9 / 63.9 MiB 是该候选在特定合成工作负载下未通过的证据，不能作为当前主线或生产结论。

源码审计能确认多项随单元行数 / 批次数增大的本仓库持有量，但不能确认它们各自的 RSS 占比。PyIceberg snapshot metadata、manifest 和扫描规划可能增加工作集；现有证据没有测得其占比，也没有证明它们是主要根因。不能把任何一个增长来源提前写成容量根因。

另有一个接口级矛盾：基线 `CanonicalUnitNormalized.revision_ids` 返回完整、按 Raw 位置排序的 ID tuple；既有门槛又把返回对象计入完整进程工作集，并要求增长不随 N / batch 数扩大。若完整 ID 输出超过门槛，不能通过只在 probe 中不持有结果或把 API 结果排除来关闭 E1；必须改为有界结果表示，并保留可显式流式读取完整 ID 的能力，或证明全量 tuple 确实落在预算内。该接口现由 ADR-0076 决定改为固定大小摘要 + 显式 stream。

## 审计证据分级

| 项 | 已由仓库代码证明 | 尚未证明 / 需要测量 |
|---|---|---|
| Raw positions | 已实现候选（本地提交 `7197973`）：`scan_column_batches` 输入、受固定 SQLite page cache 限制的磁盘排序、定长 int64 rank 文件；`_Survey` / `_UnitFacts` 不再持有 positions tuple。尚未验收 | Arrow 单批、SQLite sort、排序文件写入的峰值 RSS 和实际批大小；持久缓存资源回收 |
| Canonical committed columns | 本地提交 `b61d609` 改为 `scan_column_batches`；标量摘要保存 count/null/min/max、单一 ready 值 / 冲突标记、最多两个 distinct versions，序号和 close 精确比较写入 disk-backed index | 尚未运行测试；需验证所有重复、缺号、空值、版本与 block 异常均被拒绝，并测量 Arrow batch / SQLite / PyIceberg 的实际 RSS |
| D1 archive verification | `ParsedArchive.rows` 是整表；normalizer verifier 缓存已解析 archive；关闭缓存只会重解析，不会消除单次整表 parse 峰值 | spool / 有界解析方案能否精确保留整文件拒绝、逐行哈希和跨行语义；RSS |
| Batch history index | verifier 为 Raw batch 保留 SnapshotInfo 索引并在调用时复制；列表会随历史 batch 数增长 | 在精确保留 batch ID / 顺序 / 重复验证的前提下改为计数或窄窗口后，内存与时间收益 |
| Result IDs | result API 返回完整 ID tuple；survey / write 路径还有 ID 中间副本 | 各副本与最终公开 tuple 的实际 RSS；低于阈值的可能性 |
| PyIceberg / Iceberg history | adapter history 遍历保持 metadata snapshot 列表可达；已锁版本源码检查支持其为列表 | snapshot metadata、manifest、planner 临时对象在当前 main 探针中的实际 RSS；不能归因自候选分支的总增长值 |

详细函数位置、原语义与 E1 关闭条件仍以 [E1 复核](2026-09-27-e1-review.md)、[有界历史调查](e1-bounded-history-options.md) 为准。

## 处理决定

1. 保持 32 MiB 完整工作集门槛；不排除 PyIceberg metadata 或结果对象，不降低完整性证明要求。
2. 不把失败候选整支移入主线。其 spool、窗口证明与固定计数实现仅供逐项参考；任何复用都必须先对照当前 main 的代码，并重新满足关闭标准。
3. E1 修复按三条独立工作流设计，避免把一个猜测包装成根因：
   - **E1-R：仓库内存状态**——positions、committed 列、完整性比较、batch indexes 与临时 ID 副本改为固定窗口 / 有界 spool / 可重复有界扫描；每个缺口列出保持的拒绝语义。
   - **E1-API：返回接口**——盘点仓内消费者和预算。如果完整 `revision_ids` tuple 不能在当前门槛内保留，写 ADR 并改为有界统计结果 + 显式 ID 流式读取接口；不做隐式惰性加载或无生命周期管理的临时文件句柄。现依 ADR-0076 决定并在当前分支实施，仍待静态复核和未来测试。
   - **E1-H：Iceberg 历史工作集**——在 E1-R 后以隔离子进程设计 L / H 分离矩阵。若完整工作集仍随 H 超过门槛，再单独比较 PyIceberg 复用、Iceberg snapshot 保留语义和核心读路径替换；任何 retention / catalog / read-write 技术变化必须先走 ADR，并说明旧 pinned dataset 的可复现性。
4. 代码任务按 parser、row-integrity、catalog/normalizer 等单模块顺序拆分；涉及 `core/` 的任务不并行。跨模块协调由 Codex 负责。
5. 在用户要求的统一验收窗口前不运行测试、probe、build、lint、typecheck 或数据生成；代码可先按设计落地，但状态一律写作未验证。容量修复不能在静态复核后声称通过。

## 2026-09-28 实施进度：批次历史与流式读取

在现有 E1-R 路径内先落地基础切片，已本地提交 `de3bfdb`、`7197973`，尚未验收：

- `PyIcebergCatalogAdapter.scan_column_batches` 使用 PyIceberg 的 Arrow batch reader，并在迭代耗尽、读取异常或显式 `close()` 时关闭 reader；`PinnedCatalogView` 将请求固定到显式或绑定 snapshot，底层不支持时拒绝，不退回整表物化。
- `PersistedRowVerifier` 的 batch-history 缓存改为每个 lineage 的固定摘要；验证时第二遍从缓存的同一 history head 流式读取快照。D2 继续核连续前缀、批大小计划和 touched batch 的 fingerprint；D3E 继续核允许缺口的 index、分批行数和 fingerprint。
- `CanonicalNormalizer._positions` 改为消费 pinned batch reader，借助固定 SQLite page cache 外排位置，再写入定长 int64 rank 文件。`_Survey` / `_UnitFacts` 直接持有惰性 Sequence，不再复制完整 position list / tuple；REST 位置使用 offset membership view。SQLite 与 rank 文件由索引对象拥有，survey 路径和 facts cache 有关闭 / 淘汰清理。
- 为常数空间检测重复 index，这一切片把“同一 lineage 的历史 index 按快照时间严格递减”作为完整性约束。D2 与 REST writer 当前均按 index 递增顺序提交；旧 verifier 没有拒绝所有乱序但 index 唯一的历史。该切片将拒绝此类外部导入或手工构造的乱序历史，属于有意收窄，需在代码说明中保留此约束。

2026-09-28 又在 `b61d609` 提交 Canonical committed-row / close 的批次核验：按 pinned scan 批次读取 `arrival_seq`、`knowledge_time` 与 schema version；序号以临时 index 排序后与预期值逐项比较，close 的 unit 与 block 两次扫描都显式使用写入时的 `snapshot_id`。ready time 和 schema version 只保留摘要。**批次 API 不代表完整扫描内存有界**：对锁定的 PyIceberg 0.12.0 源码复核发现 planner 会构造完整 manifest entry、delete index 与 `FileScanTask` 集合；Arrow 路径还会汇总 delete arrays 并在 task 中物化批次列表。当前开发分支为 infrastructure `RevisionCatalog` 增加了 `scan_column_batches` 声明，并给 revision / manifest-cache 测试代理补了委托和读取记录；仍有测试引用已删除的 `_same_numbers`。这些改动尚未验证，当前不能声称扫描对行数 / 历史有硬上界。默认 tempfile 位置在本机 `/tmp`，该挂载是 tmpfs，文件 spill 仍可能计入主机/cgroup 内存。全 null seq 明确抛出 `CatalogIntegrityError`；超过两个 distinct schema versions 时诊断只报告保留的两个值，但仍 fail closed。上述均未运行测试、probe、build、lint、typecheck，代码和资源路径未验收。

随后本地整合分支增加 `64b021a`、`1d8f231`、`62bfc9d`、`3997e0b`：在 `normalize_unit` 结果路径从同一 pin 重建已提交 revision IDs，避免在 survey 中再保留第二份 ID tuple；Verifier / REST 单 batch 路径以 SQLite 保存请求 batch 的精确匹配数和首个最新 snapshot，并保持公开 `snapshots_of_batches` dict/list API。重建仍要返回 O(N) ID list / tuple，spool 仍需遍历整段 history；新 helper 不改变 PyIceberg planner/delete/task 工作集。公开 API 兼容性经独立静态复核后恢复。仅 `git diff --check` 与源码审阅；未运行测试或容量探针。

仍有增长状态：可选保留的 committed rows、每个 microbatch 的 planned rows、D1 完整 archive bytes / `ParsedArchive.rows`、history caller 集合。ADR-0076 的默认 normalizer result 已改为摘要；显式 ID iterator 重读并重证单个 bounded microbatch。当前 ADR-0075 scan 改为逐 entry / 文件处理，`max_int64` 也复用该固定快照扫描；锁定 PyIceberg AvroFile 会将完整 manifest-list 与一个 manifest bytes 读入内存。普通 catalog fallback 的 history cycle detector 已改为 Brent O(1) 辅助空间；在线检测在完整遍历周期之前可能先 yield 重复节点，完整消费仍会拒绝该周期。DatasetBuilder replay manifest 检查已改为流式早停，但 `DatasetBuilder.select` 与 v2 `ResearchDatasetManifest` 的完整集合仍随输出增长；Raphael 已批准起草 ADR-0077 并按新契约实现，旧 manifest 保留只读兼容。Arrow row group / stripe、SQLite sort、tmpfs 文件、PyIceberg / Iceberg metadata 的完整工作集尚未测或证明有界。E1-CAP-1 仍阻断，32 MiB 门槛没有通过证据。

## E1-R 实施和后续验收边界

仓库内可直接研究的实现边界：

- 固定范围 scan 必须以位置范围而不是仅减少列宽；窗口间仍需验证 `archive_line_number = 1..N`、REST index 连续性、重复 position 和单位排名切片。
- Canonical 严格核验按稳定固定 snapshot 分块，跨块累计只保留固定计数 / 摘要；重复序号、缺失序号、knowledge time 合法性、schema version 唯一性、batch prefix / fingerprint 与崩溃恢复必须 fail closed。
- archive spool 不能把被拒绝文件变成部分成功；须保留完整 ZIP / CSV 格式与 checksum 验证、重复记录、整文件结束状态、逐行规范化哈希和关闭后清理。
- batch 索引只有在不丢失 duplicate batch、连续 prefix、批次身份和来源绑定拒绝能力时，才可改为计数摘要。
- `CanonicalUnitNormalized` 的 API 输出不能从容量统计中静默剔除。仓内消费者盘点见 ADR-0076；默认 result 已改为摘要，需完整 IDs 时显式按 Raw position 顺序迭代，不把 ID 输出从容量口径排除。

### 2026-09-28 ADR-0075 / 0076 当前开发进度

- ADR-0075 已在 `infrastructure/catalog/iceberg_adapter.py` 实现候选：固定 snapshot，两遍扫描 manifest entries，先完整拒绝 delete / unknown 内容，再逐文件 yield Arrow batches；`max_int64` 复用该 bounded path，不再用 PyIceberg 高层 planner / task list。Parquet / ORC 沿用 Arrow decode；不支持的 live data file 在 yield 前 fail closed。尚未测试结果一致性和提前关闭。
- PyIceberg 0.12.0 `AvroFile.__enter__` 对 manifest-list 与当前 manifest 执行完整 `f.read()`；Arrow 依 row group / stripe 解码，batch row 数不是 byte 上界。该实现不构成 E1-CAP-1 通过证据。
- ADR-0076 已接受；默认 `CanonicalUnitNormalized` 只存固定大小 counts / replay 摘要，`iter_revision_ids` 重证 unit 后按 Raw position 顺序逐 microbatch 产出。Normalizer capacity / DNET 工具已改读摘要；Canonical 测试已迁移到摘要与显式 ID stream，包括 AUD-1 M3 helper 改名 / 签名同步，未运行。
- D1 的 `BytesIO(data)` 自定义 reader 替换已撤回：Claude 源码复核指出只读 BytesIO 在 CPython 上可能共享不可变 bytes 缓冲区，改用自定义 reader 的 RSS 收益未经证明。`_ColumnBuffer.table()` 仍移除 `combine_chunks()` 全量列合并，返回 Table 可以保留多个 Arrow chunks。其后 `parse_archive()` 的 StorageAdapter 入口改用固定块 seekable tempfile（见下一条）；`parse_archive_bytes()` 仍使用调用方提供的 bytes。
- 后续 D1 基础实现将 `parse_archive()` 的 StorageAdapter 读取按固定块同步哈希并写入 seekable `TemporaryFile`，验证长度 / SHA-256 后从同一 spool 解析；对外 `ParsedArchive` / rows 原子性未改，`parse_archive_bytes()` 保留。此举去除 storage 入口下同时常驻的完整 Python 输入 bytes 副本，但默认临时目录可能是 tmpfs，完整 `ParsedArchive.rows` 仍随结果增长；不宣称完整工作集有界，未测试。
- E1 probe 的每个子进程现在把 `TMPDIR` 定向到同一次测量的 per-run workdir；workdir 位于 tmpfs / ramfs 时原有拒绝逻辑会阻止运行。Parser spool、Canonical positions 和其它 `tempfile` 文件因此与被检查的 workdir 共用 filesystem，避免默认 `/tmp` 绕过 tmpfs 检查。该变更尚未运行或验证。
- DatasetBuilder 审计发现 `DatasetSelection.rows` / `lineage` 与 `ResearchDatasetManifest.lineage` 是完整公开 / 冻结 tuple，`build` / `verify_manifest` 还构造完整 Arrow Table 和对照结果。只改 `seen_keys` 存储或新增 iterator 无法移除完整返回对象的 O(N)。Raphael 已批准起草 ADR-0077 并按新契约实现；提案须规定完整外置证据与行数据如何有界持久化、校验及兼容旧 manifest，不能以局部 spill 冒充有界。
- 正常化热路径原先仍从 `_raw_window`、`_check_unique`、`_scan_block` 调用高层 `scan_columns`，会重建 O(files) planner/task 集合，ADR-0075 没有覆盖这些调用。当前工作区将 Normalizer 与 `PersistedRowVerifier` 的窄查询都改为 `scan_column_batches`，只保留一批 reader；窗口结果和异常额外行都受请求范围限制。`normalizer_memory_probe --staged-diagnostics` 现同时计数 bounded scanner 两遍访问的 manifest-list entries / live entries / data-file reads，以及仍走高层 planner 的 manifest / task；并统计 `scan_column_batches` / `max_int64`。计数器统计条目和启动的读取，不统计 bytes，manifest-list bytes 没有单独计数。此前仅钩住 `ManifestGroupPlanner.plan_files` 会漏掉 bounded scanner 的工作，现已补回。Arrow scanner iterator 在外层提前 close 时先尝试关闭，再释放仍打开的输入文件；Verifier 仍拒绝非正整数 scan limit。上述改动还未测试或容量测量。
- 上述候选改动及本轮测试同步未运行测试、probe、build、lint、typecheck 或验收；当前状态仅经静态检查，容量仍未知。

正式容量验收继续遵循 E1 review：当前代码线、M=256、N=10k/100k/500k、隔离子进程和重复运行；每个阶段增长不超过 32 MiB；记录 baseline、采样间隔、L/H、Catalog 调用与返回对象；同时用递归结构测试证明长期容器基数边界。该设计文档本身不是实现、测量或验收记录。

### 2026-09-28 PyIceberg scan / spool 复核补充

- 锁定版本为 PyIceberg 0.12.0。源码路径通过 `Snapshot.manifests()`、完整 manifest entry 列表、delete index、完整 task list，最后由 ArrowScan 汇总 delete 内容并将 task 结果批次列表化；降低 Arrow batch 或 worker 数不能构成全路径固定内存上限。
- 初始审计未找到配置级方案。后续 ADR-0075 已接受 adapter 内逐 manifest / entry / file 的 pinned-snapshot 读取；当前实现拒绝 delete manifests/files，因此暂不实现可 spill delete index。该路径仍依赖锁定版 PyIceberg 私有原语，且 Avro manifest bytes 与 row-group / stripe 字节上界未解决；须经后续测试与容量测量，不能称为硬容量界或 E1 通过。
- 本机默认 tempfile 落在 tmpfs。统一容量测量必须把临时文件所在 filesystem 纳入说明，并记录完整 cgroup memory 与进程 RSS；不得将 tmpfs 文件称为 RAM 外 spill。
- `scan_column_batches` infrastructure Protocol / 测试代理接线已在 `34b95c3` 完成；ADR-0075 Amendment 1 与 ADR-0076 已接受，当前工作树实现固定快照扫描及摘要 / 显式 ID stream。Normalizer 和 row-integrity 测试代理已切换到批次 API；M3 的 `_same_index_numbers` 调用与旧结果字段断言已更新。变更未运行测试或类型检查。

### 2026-09-30 pinned PyIceberg 0.12.0 source check

Read-only source review against the pinned upstream release confirmed that `TableMetadata.snapshots`, `snapshot_log`, and `metadata_log` are Python lists, and `snapshot_by_id` linearly iterates `snapshots`. The standard SQL Catalog load / commit path remains outside ADR-0075's bounded read adapter and belongs in the complete-process measurement. These source facts establish growing data structures, not their exact RSS multiplier or a root cause for any particular probe result.

- Pinned upstream sources: [PyIceberg 0.12.0 metadata model](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/table/metadata.py#L1872-L1905), [`snapshot_by_id`](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/table/metadata.py#L1963-L1968), [SQL Catalog](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/catalog/sql.py), and the [Iceberg table-metadata specification](https://iceberg.apache.org/spec/#table-metadata).
- Disposition: keep FULL_PROCESS_WORKSET and the 32 MiB gate unchanged. Do not adopt snapshot expiration, history caps, custom metadata commits, or a replacement catalog/read-write engine without a separate ADR. Measure the current integration line after repository-owned O(N) graph, Dataset, Normalizer, and verifier work is closed; use those measurements before proposing changes to supported history or metadata writes.
- This is not E1-CAP-1 evidence and does not attribute a memory failure to PyIceberg. Release context: [Apache Iceberg Python 0.12.0 release](https://iceberg.apache.org/blog/apache-iceberg-python-0.12.0-release/).
