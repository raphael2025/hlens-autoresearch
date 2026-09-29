# ADR-0077: 有界 Research Dataset 证据——v3 manifest、内容寻址有序 evidence streams 与定长 chunk commit

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28：DQ-1 = A 由 Raphael 亲自批准；DQ-2～DQ-8、DQ-10～DQ-12 由 Codex 决定；DQ-9 **OPEN**，待容量证据）。契约层已实现（见「实施记录：契约层」），infrastructure 层待实施；§6.1 / §6.2 仍是 infrastructure 实施前的强制门 |
| 日期 | 2026-09-28 |
| 决策者 | Raphael（DQ-1）；Codex（依 Raphael 2026-09-28 授权决定 DQ-2～DQ-8、DQ-10～DQ-12） |
| 起草者 | Claude Code（Opus），docs-only；未运行任何测试、构建、lint、typecheck、Python 或 probe |
| 相关 Phase | Phase 1 — Market Representation（F3 / E1-CAP-1 的 Dataset 部分） |
| 影响范围 | Contract（需新增 `core/contracts/universe.py` 模型并提升契约版本）/ Data（新增生产表、evidence 对象）/ Infrastructure（`infrastructure/dataset/`） |
| 是否破坏兼容 | 推荐方案（DQ-1 = A）对已发布契约为 **additive minor 2.3.0**：`ResearchDatasetManifest` 与既有 15 张表定义不变；`DatasetBuilder` / `DatasetSelection` / `DatasetBuilt` 这组 infrastructure API **破坏性**变更；已持久化 v2 manifest 只读兼容（硬要求） |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md) §6 / §7、[ADR-0024](0024-historical-tradable-universe.md) §6、[ADR-0033](0033-research-dataset-selection-table.md)、[ADR-0052](0052-validation-contract-completion.md)（按记录版本重放）、[ADR-0075](0075-bounded-pyiceberg-snapshot-scan.md)、[ADR-0076](0076-bounded-canonical-normalization-result.md) |

## 背景（Context）

### 已批准的范围

Raphael 于 2026-09-28 批准 [数据集有界结果决策包](../reviews/2026-09-28-dataset-bounded-result-decision.md) 的方案 B：
起草 ADR 并**按新契约实现**有界 Dataset selection / build；**已持久化 v2 manifest 的只读兼容是硬要求**；
不得以局部 spill 冒充有界，不得削弱重复拒绝、lineage 完整性、manifest 完整性或行校验（H4）。
本 ADR 把该路线写成可执行方案；**方案内的各个子选择尚未决定**，全部列在「决策问题」中，由 Codex 复核。

### 现有实现为什么随 N 增长

| 位置 | 随 N（选中行数）或窗口增长的持有量 |
|---|---|
| `DatasetSelection.rows` / `.lineage` / `.evidence_gaps` | 完整 tuple；`DatasetBuilt` 持有 selection |
| `DatasetBuilder.select` 的 `seen_keys`、`lineage` dict、`gaps` / `gap_partition` dict、`partitions` 日集合 | 跨 slice 累积 |
| `_materialize` | 一次构造完整 `pa.Table`、`to_pylist()` 排序后的全量 expected、全量读回再排序比较 |
| `verify_manifest` | 重新 `select` 全量，再构造完整 Arrow Table 算指纹 |
| `ResearchDatasetManifest.lineage` / `.evidence_gaps` | 冻结契约中的完整 tuple；首切片每个选中成交 revision 通常都有证据缺口（D-HIST，ADR-0032 未绑定时一律缺口，绑定时仍"缺口照列"），因此两者都是 O(N) |
| `ResearchDatasetManifest.quality_report_ids` 与 `research.dataset_manifests.quality_report_ids` 列 | 每个 (member symbol × UTC 日) 一个报告：O(symbols × 窗口天数)，不随行数但随窗口无界增长；表列是一行内的 `list<string>` |
| `manifest_json` 列 | 整份 manifest 规范 JSON 一个字符串：O(N) |
| `research.dataset_selections` | 行无 ordinal、无 chunk 列；分区为 `identity(symbol) + day(event_time)`，Iceberg 扫描顺序不是构建顺序，读回只能全量排序 |

v2 manifest 的内容哈希是**整份规范 JSON 的 SHA-256**：要得到它必须一次性序列化全部 lineage / 缺口，这与固定工作集不相容。
因此"把 tuple 换成 iterator"或"把 `seen_keys` 放进 SQLite"都不够：manifest 的身份定义本身必须改为对外部有序证据的**承诺**（commitment）。

### 约束

- ADR-0023 §6：Research Dataset 必须绑定 manifest；只有 snapshot id 不足以证明逐行 PIT；缺 manifest 不得进入实验。不升 3.0.0 是该 ADR 当时的裁决。
- ADR-0024 §6：manifest 须绑定成员清单与排除原因清单。
- ADR-0023 §7：partitioned 表只用有界、可重建的 `pyarrow.Table` microbatch 写入；每 batch 有稳定 id；orphan 只由显式 maintenance 清理。
- 02-domain §3 第 3 条：minor 只能添加可选字段 / 新模型；删除、重命名、语义变化 = major + ADR + 迁移。D-25：2.0.0 已发布。
- ADR-0052 versioned replay：持久化对象按记录版本重建；新组按当前版本写；code-registered 身份保持其发布信封。
- `StorageAdapter.stage` 要求调用前给出 `expected_sha256`（`core/contracts/storage.py`）；没有覆盖 / 删除 API。
- E1-CAP-1：完整进程工作集计入 32 MiB 门槛，门槛不变；ADR-0075 / 0076 已声明的未闭合项（Avro manifest bytes、row group / stripe、Iceberg metadata、tmpfs）继续有效。

## 决策（Decision）

标注 **[DQ-n]** 的位置对应「决策问题」中的子方案；正文按「推荐」撰写，除 DQ-9（数值，OPEN）外均已按推荐决定
（见「决定记录」）。

### 1. 新 manifest 形态（v3）与契约模型

1. 新增契约模型 `ResearchDatasetEvidenceManifest`（下称 **v3 manifest**；"v3" 是 manifest 形态代号，**不是**契约 major），与 `ResearchDatasetManifest`（v2 形态）并存。v2 模型字段、校验、Schema、内容哈希**逐位不变**。[DQ-1]
2. v3 manifest 只含**固定大小**字段（与 N、窗口天数无关），全部必填：
   - `dataset: DatasetRef`（zone = `research_dataset`；`snapshot_id` = 最后一个 chunk commit 的 snapshot，见 §4）；
   - `point_in_time: PointInTimeSpec`（与 v2 相同，仍是上游 snapshot / cutoff / policy / parser 绑定的唯一来源；其大小随绑定的表与 policy 数，而非 N）；
   - `universe_spec: UniverseSpecBinding`；
   - `rule: DatasetRuleBinding`（`rule_id + SemVer + rule_hash`，新 dataset 规则 `hlens.dataset.pit-selection@2.0.0`，其 spec 中写明排序、chunk、leaf、fan-out 参数）；
   - `data_type`（v2 需在验证时穷举推断，v3 显式记录）；`selection_id`；
   - `row_count`、`chunk_rows`、`chunk_count`（`chunk_count = ceil(row_count / chunk_rows)`，末块可短，其余块恰为 `chunk_rows`）；
   - `evidence: tuple[EvidenceStreamRef, ...]`：**恰好**每种 stream 一项（§2 的六种），按 stream 名排序。
3. `EvidenceStreamRef`：`stream`（枚举）、`format`（`hlens.dataset.evidence-jsonl@1.0.0`）、`record_count`、`leaf_count`、`depth`、`root`（`EvidenceObjectRef`）。
4. `EvidenceObjectRef` 只绑定内容身份：`key`、`sha256`、`size`；**不**把实现生成的 `uri` 纳入 manifest 内容哈希。[DQ-8]
5. 契约层可证明的不变量：`dataset` 形状与 v2 相同规则；`selection_id` 与 `rule` / PIT / universe / `data_type` / 窗口的派生关系（纯函数，可在契约或 infrastructure 校验，实现批次定）；计数一致（`chunk_count`、chunk-proof stream 的 `record_count == chunk_count`、lineage stream 非空等）；stream 集合完整且不重复；`root.key` 与 `root.sha256` 满足 §3 的键规则。与 v2 一样，**契约只证明结构**：对象是否存在、字节是否匹配、内容是否就是输入的派生，属存储 / verifier（§6）。
6. 流内记录复用既有 v2 模型作为**记录模型**：`UniverseMember`、`UniverseExclusion`、`SelectedRevisionLineage`、`AvailabilityEvidenceGap`；新增 `DatasetQualityReportRef`（`report_id`、`subject`：`listing` 或 `(symbol, day)`）与 `DatasetChunkProof`（§4）。记录行的信封处理见 [DQ-7]。

### 2. 内容寻址的有序 JSONL evidence streams

六种 stream，每种一棵独立的承诺树（§3）：

| stream | 记录 | 规范顺序（ordinal 递增） | 去重 / 唯一性如何在有界内存中证明 |
|---|---|---|---|
| `members` | `UniverseMember` | `(episode.observation_key(), effective_from)`（与 v2 排序键相同） | 相邻比较：同键重复或区间重叠即拒绝 |
| `exclusions` | `UniverseExclusion` | 同上 | 同上；"同时既是成员又被排除"用 members / exclusions 两流按同一键归并扫描检查（双指针，O(1)） |
| `lineage` | `SelectedRevisionLineage` | 先 listing lineage（按 `canonical_revision_id`），再数据 lineage 按**该 revision 首次出现的行 ordinal** | revision 只属于一个 `observation_key`；同一 key 的行在行序中相邻（§4 排序），故只需在 key 组内去重 |
| `evidence_gaps` | `AvailabilityEvidenceGap` | 同 lineage 的顺序规则 | 同上 |
| `quality_reports` | `DatasetQualityReportRef` | `listing` 在前，再按 `(symbol, day)` | 严格递增即唯一 |
| `chunk_proofs` | `DatasetChunkProof` | `chunk_index` | 从 0 连续 |

- **行序（dataset rows 与数据 lineage / gaps 的共同基准）**：v3 采用**生成顺序**——`(member symbol 升序, slice 序号, observation_key, effective_from, revision_id)`，并为每行分配全局 `row_ordinal`（0 起连续）。v2 的 `_row_order` 是跨 slice 的全局 `observation_key` 字符串序，需要外部排序才能流式产出，故不沿用。[DQ-4]
- **每行 JSONL**：`canonical_json(record) + "\n"`，UTF-8，无 BOM；空 stream 合法（`record_count = 0`，根为只有 header 的空索引对象），但 `lineage` / `quality_reports` / `chunk_proofs` 按规则必非空。
- **跨 slice 的 observation_key 唯一性**（v2 用 O(N) 的 `seen_keys`）：推荐以**本地所有权复核**替代——每个被选 key 必须满足 PIT selector 现有的 slice 所有权规则（由链上最早 revision 所在窗口选中，D-F1n ⑨），在 slice 内即可判定，且 verifier 独立重算。任何 key 被两个 slice 认领仍 fail closed。[DQ-5]
- **报告与缺口绑定的顺序**：每个 symbol 的 slice 按时间推进，(symbol, day) 的报告在该日最后一个 slice 处理完后确定；该日的缺口按 §2 规则写入 `evidence_gaps`，同时核对报告记录（沿用 `evidence_gaps_of` 在 pinned view 上读取，ADR-0031）。covered day 集合为"窗口日 ∪ 已选 revision 的事件日"，按 symbol 顺序生成、在日粒度上有界于窗口长度；若出现窗口外事件日，按日序并入，实现须证明其单调或 fail closed（实现批次核实选中行 `event_time` 是否可能落在窗口外）。

### 3. 固定内存的叶对象 / 索引树、对象键与哈希

1. **叶对象**：一个 stream 的连续记录段。首行为 header `{"format", "stream", "node": "leaf", "first_ordinal", "record_count"}` 的规范 JSON（域分离：相同记录字节在不同 stream 或层级下哈希不同），其后为记录行。切分规则确定：记录数达到 `leaf_max_records`，或再加一行会超过 `leaf_max_bytes` 时切分；**单条记录超过 `leaf_max_bytes` 即 fail closed**，不截断。
2. **索引对象**：header `{"format", "stream", "node": "index", "level", "first_ordinal", "record_count"}` + 至多 `fanout` 行子引用 `{"key", "sha256", "size", "first_ordinal", "record_count"}`。按层自底向上构建：每层只保留当前未满的一个索引缓冲，构建期内存 O(`fanout` × `depth`)；`depth = ceil(log_fanout(leaf_count))`（≥ 1，单叶也由一个根索引包裹，便于统一校验）。
3. **哈希**：对象身份 = 对象完整字节的 SHA-256（小写 hex）。根对象哈希进入 v3 manifest，从而经 manifest 内容哈希承诺整条 stream 的**顺序、计数与内容**；子引用携带 `first_ordinal` / `record_count`，截断、重排、插入都会在父节点处被发现。
4. **对象键**：推荐 `research/dataset-evidence/v1/<sha256>.jsonl`（只由内容哈希决定，天然幂等、跨数据集去重；满足 `OBJECT_KEY_PATTERN`）。[DQ-6]
5. **写入**：每个叶 / 索引对象在内存中组装（受 `leaf_max_bytes` / fan-out 限制），先算 SHA-256，再 `stage(expected_sha256=…)` → `publish`；已存在同内容即 `already_present`（幂等）。**不**把整条 stream spool 到临时文件求哈希，也不修改 `StorageAdapter` 契约。[DQ-11]
6. 参数 `chunk_rows`、`leaf_max_records`、`leaf_max_bytes`、`fanout` 写入 `hlens.dataset.pit-selection@2.0.0` 的规则 spec（进入 `rule_hash`，改值 = 新规则版本）。它们是工程资源参数，不是验证阈值；具体数值须按 E1-CAP-1 测量选定。[DQ-9]

### 4. Dataset 行的定长 chunk commit 与断点重放

1. 新增生产表（推荐名 `research.dataset_selection_chunks`，名称待定）：v2 八列 + `chunk_index`（long，必填）+ `row_ordinal`（long，必填，全局 0 起连续）。分区推荐沿用 `identity(symbol) + day(event_time)`（与表族一致、复用 ADR-0026 写路径）。[DQ-3]
2. 行按 §2 行序生成；每累计 `chunk_rows` 行提交一个 batch，`batch_id = "<selection_id>.chunk-<chunk_index 十位零填充>"`（满足 `BATCH_ID_PATTERN`），`row_count` 与 `hlens.pyarrow-batch-sha256@1.0.0` 指纹按该 chunk 的 Arrow Table（行按 `row_ordinal` 升序）计算。每次提交后以固定 snapshot 读回**该 chunk**（`selection_id` 与 `chunk_index` 过滤，经 ADR-0075 有界扫描），在 chunk 内按 `row_ordinal` 排序后逐行比较——排序代价受 `chunk_rows` 约束。
3. 每个 chunk 追加一条 `DatasetChunkProof`：`chunk_index`、`batch_id`、`snapshot_id`、`first_row_ordinal`、`row_count`、`batch_fingerprint`。v3 manifest 的 `dataset.snapshot_id` = 最后一个 chunk 的 snapshot；该 snapshot 上 `selection_id` 的行集合恰为全部 chunk 之和（Iceberg snapshot 是累积的）。
4. **断点重放**：构建确定，不持久化游标。重跑时从头重新派生（O(N) 时间、固定工作集），对每个 chunk 先按 `(table, batch_id)` 查已提交快照：已提交则核对指纹、行数与读回内容后跳过；未提交则提交；第一个缺失 chunk 之后不得存在已提交 chunk（空洞 = 完整性错误）。已提交 chunk 与重新派生不一致即 `CatalogIntegrityError`，绝不覆盖或绕过。evidence 对象同理：内容寻址，重放即 `already_present`。
5. 并发：沿用 ADR-0023 §7 单 writer；`expected_parent_snapshot_id` 冲突按同一 `batch_id` 幂等重试。其他构建的 batch 可插在 chunk 之间，读取以 `selection_id` 过滤，不影响正确性。
6. 空选择：沿用 v2 规则，拒绝（无自身 snapshot 可绑定）。[DQ-12]
7. 代价（须如实记录）：每个 dataset 产生 `chunk_count` 个 Iceberg snapshot，表 metadata 的 snapshot 数 H 随之增长，与 E1-H（PyIceberg `TableMetadata.snapshots` 的 O(H)）直接相关；`chunk_rows` 越大 snapshot 越少但单 chunk 工作集越大。

### 5. 显式有序的 evidence / rows API（infrastructure）

沿用 ADR-0076 的原则：默认结果固定大小，完整内容只经显式、保序、可关闭的迭代器读取，不设隐式惰性属性、不在结果对象上挂文件句柄。

- `DatasetBuilder.build(...) -> DatasetBuildSummary`：固定大小（`selection_id`、v3 manifest 内容哈希、`dataset` ref、`row_count`、`chunk_count`、`replayed_chunk_count`、各 stream 的 `record_count` 与根哈希、`replayed`）。不返回 rows / lineage / gaps tuple。
- `DatasetBuilder.select(...)` 的全量 `DatasetSelection` 不再作为 v3 的公共结果；只读推导改由内部生成器驱动，外部需要逐项内容时使用下列迭代器。
- `iter_evidence(manifest, stream) -> ContextManager[Iterator[record]]`：自根向下深度优先读取对象（`open_read` 已核对 sha256 与长度），逐节点核对 header、子引用的 `first_ordinal` / `record_count` 连续，按 ordinal 顺序产出记录模型；任一不符即抛错。每条产出的记录都经根哈希认证；截断在父节点即被发现。该迭代器**只证明"这就是 manifest 承诺的流"**，不证明语义正确（那是 §6）。
- `iter_rows(manifest) -> ContextManager[Iterator[row]]`：按 `chunk_proofs` 逐 chunk 在其 snapshot 读取、chunk 内按 `row_ordinal` 排序后产出，核对连续 ordinal 与每 chunk 行数 / 指纹。
- 迭代器在自然耗尽、显式关闭、异常与调用方提前停止时释放 reader 与对象句柄。调用方持有的产出物计入其自身工作集，不得通过"消费者不持有"来宣称容量通过（与 ADR-0076 §5 同一口径）。

### 6. Streaming verifier：逐条重证完整性

`verify_manifest` 对 v3 manifest（`ManifestStore.persist` 前、`load` 后都调用，与 v2 相同）：

1. 契约校验、`selection_id` 由 manifest 输入重算相等、`rule` 为已登记规则、universe 为已登记 spec（同 v2 的 `REGISTERED_UNIVERSES` 检查）。
2. 以 manifest 的记录版本作用域（ADR-0052 V7）**重新运行同一个有界生成器**，并与 §5 的 `iter_evidence` / `iter_rows` 做**逐条归并比较**：每种 stream 的每条记录字节相等、计数相等、两侧同时耗尽；每个 chunk 的重新派生行与读回行逐行相等、指纹与 `DatasetChunkProof` / 快照摘要相等。
3. 完整性检查不削弱：重复 / 重叠 / 成员与排除冲突、listing revision 被多个 episode 认领、listing revision 必须有 listing lineage、缺口表必须有上游绑定、缺口引用的报告必须出现在 `quality_reports` 流、每个报告在绑定 snapshot 上可由其 reporter 以 `existing_only` 重导出（与 v2 相同）。v2 契约在模型内用集合完成的跨字段检查，在 v3 中改由 verifier 以**有序归并**完成：
   - "listing revision 只属一个 episode"：members / exclusions 按 episode 键有序，同一 revision id 可跨 episode 出现的情形用 listing lineage 流（按 revision id 有序）归并核对——每个被引用 id 在 lineage 中恰有一项，且其 `observation_key` 与引用它的 episode 相同（需在 lineage 记录或 listing 表中取得 episode；实现批次选定取法，不得退化为全量集合）；
   - "缺口引用的报告在报告流中"：缺口按行序、报告按 (symbol, day) 序，按 (symbol, day) 归并。
4. 没有多余数据：manifest 绑定 snapshot 上 `selection_id` 的行数 = `row_count`（有界扫描计数）；该表 snapshot 历史中以 `<selection_id>.chunk-` 为前缀的 batch 恰为 `0..chunk_count-1`（经 ADR-0075 / 现有有界 history 路径检查，不物化历史列表）。
5. 工作集：一个 slice 的 selector 结果 + 一个 chunk + 每个 stream 一个叶缓冲与 `depth × fanout` 的索引栈（写 / 读各一套）+ 固定大小的归并状态。**不**包括 N、行数或窗口天数的线性集合。

### 6.1 上游生成器是必需实施范围

上面的工作集声明不能以“一个 slice”为界。当前一个 slice 仍可包含任意多行；`PitSelector.select()` 与 `UniverseBuilder.build()` 都会返回随输入规模增长的完整对象。若只改 DatasetBuilder 与持久化层，仍会在进入 chunk writer 前持有 O(N) 数据，不能满足本 ADR 的目标。因此以下项是 v3 实施的**强制范围**，不得作为 ADR 接受后的延期项：

1. `UniverseBuilder` 改为按 `(symbol, evaluation_instant)` 顺序逐项产生成员 / 排除 / lineage / gap；输入的 exchange-info 与 listing 变化流按时间归并读取，不能先 `.to_pylist()` 成完整事件集合，也不能保留完整 `timelines`、`members`、`exclusions`、`member_spans`。既有 `build()` / `UniverseBuilt` 仅保留给 v2 legacy replay；v3 使用新的显式关闭 cursor / iterator。
2. `PitSelector` 改为固定工作集生成器：canonical scan 的任意文件顺序先经固定容量排序 run，再按 `(symbol, observation_key, revision_id)` 有序归并；处理完一个 key 即释放该 key 的内存状态。单个 key 的历史长度也不设为隐式上界，超过固定 record buffer 的内容必须通过同一 content-addressed bounded-run 格式继续分段读取；不能把“一个 key / 一个 slice”当成容量上界。
3. 排序 run 与大型单 key 历史 run 使用与 evidence tree 相同的固定字节对象写入规则：每个对象先在固定上界内组装并计算 hash，再经现有 `StorageAdapter.stage(expected_sha256=…)` 发布；多路归并的 fan-out 有上限，层次索引不在进程中收集全部 run refs。运行失败产生的对象是不可被 manifest 引用的 orphan；写路径不得删除。
4. 生成顺序、跨 slice key 所有权、selection、lineage、缺口与质量证据都必须在相邻有序流上归并验证。v3 路径禁止 O(N) 的 `seen_keys`、`by_key`、`records_by_key`、`selected_rows`、`timelines`、`member_spans`、报告 ID 集合或同等映射；v2 replay 可以保留原实现并明确不属于有界路径。
5. quality reporter 的 `existing_only` 重导出也必须可逐行读取 / 比较，或证明其单个 `(symbol, day)` 输出有契约内的固定上界；否则 v3 verifier 不能声称完整工作集有界。不得以当前样例较小作为证明。

排序 run 是可寻址的数据对象，不是隐式 OS 临时文件。运行路径不得调用系统默认临时目录，也不得假定 tempfile 位于物理磁盘；run 的固定对象上界、索引层数、对象数量和 orphan 义务都计入 E1-CAP-1 资源说明。若实际 StorageAdapter 无法在不聚合所有 run refs 的情况下提供有序多路归并，必须停止并提出新的 Decision Packet，不得回退成 slice 级全量 materialization。

### 6.2 实施前必须封闭的协议细节

只读复核确认证据树、定长 chunk、独立 v3 manifest 表、保留 v2 只读路径可以继续作为设计基础；以下不是验收阶段的测试问题，而是进入契约 / 实现前必须在本 ADR 定义清楚的协议条件：

1. **v3 对象可见性**：Catalog 无跨表事务。无 manifest 的 chunk 可从 Iceberg 表直接扫描，因此“不可用”必须由所有消费者遵守的读取规则保证：P2+ 研究消费者只能通过已加载、完整验证的 v3 manifest `iter_rows`；不得直接消费 chunk 表。Manifest 发布是唯一 commit point；验证失败或无 manifest 的 chunk 永不返回给消费者。同步盘点并迁移现有直接读 `research.dataset_selections` 的调用方。
2. **跨版本 partial replay**：当前已发布契约为 2.2.0。chunk 在 manifest 前提交，故崩溃后的 partial prefix 必须能按 ADR-0052 V1 重建。ADR 必须选择并证明：持久化写入组版本，或证明 selection rule/version 使所有 chunk 字节在 2.x → 2.3.0 间完全稳定；仅依赖尚未存在的 manifest 不成立。实施前完成 code-registered universe / PIT identity 的 2.3.0 replay 盘点。禁止以“后续统一测试”替代该设计。
3. **唯一序列化投影**：evidence JSONL 必须指定字段集合、字段次序规则、日期 / enum / Decimal 表示、UTF-8 与换行规则，以及 `schema_version` 是否进入记录字节。若记录字节不含信封，须明确在来源契约版本作用域内重建；不得同时引用默认 `model_dump(mode="json")` 与不含 `schema_version` 的规则。
4. **引用解析**：reader 对每个 `EvidenceObjectRef` 先调用 `StorageAdapter.lookup(key)`，逐项比较返回对象的 key、sha256、size 与 manifest / 父索引，再 `open_read`。不得依赖 `open_read` 单独证明引用身份。
5. **既有 ADR 衔接**：更新 ADR-0023 §6 对“manifest 绑定成员 / 排除清单”的描述，明确 v3 根哈希承诺仍满足该义务；在 ADR-0033 补充 v3 新 selection 表路径，并重申 v2 的 `DatasetRef.table` 与 schema 不变。新表名称、schema、分区和 definition hash 必须在接受前冻结。
6. **历史扫描限制**：chunk batch 历史检查只使用 infrastructure 内部能力和逆序流式常量状态；不扩展冻结的 public `CatalogAdapter` Protocol。当前 PyIceberg metadata 的 snapshot 集合仍是 O(H)，故只能声称仓库显式 Python 集合有界，不能声称进程总工作集 O(1) 或 E1-CAP-1 通过。

上述任一点无法在现有 ADR / adapter 边界内定义时，保持 Proposed 并提出单项 Decision Packet；不得先写代码再补协议。

### 7. 持久化：manifest 表、`quality_report_ids` 列与 selection 行的 ordinal

- 现有 `research.dataset_manifests`：`quality_report_ids` 是必填 `list<string>`（文档写作 "set, sorted"），`manifest_json` 是整份 manifest。v3 manifest 若写入该表，要么把报告列表继续内联（违背有界目标），要么写空列表（与列文档"引用的报告集合"语义冲突，且 v2 `manifest_row` 等值比较路径会误判）。
- 现有 `research.dataset_selections`：无 `chunk_index` / `row_ordinal`；在其上做 schema evolution 会改变冻结表定义与 `_check_dataset_table` 的精确 schema 检查，并使 v2 批次的重放指纹（按当前 Arrow schema 构造）不再相等，直接危及 v2 只读重放硬要求。
- 因此推荐：**两张新表**，既有两张表定义、分区与哈希不变、只读保留：
  - 新 manifest 表（推荐名 `research.dataset_evidence_manifests`，不分区）：只有固定大小列——`manifest_content_hash`、`contract_schema_version`、dataset 身份列、`selection_id`、rule 三元组、PIT 三元组与时间列、universe 三元组、`snapshot_bindings`（有界于绑定表数）、`data_type`、`row_count`、`chunk_rows`、`chunk_count`、每 stream 一组（`stream`、`record_count`、`leaf_count`、`depth`、`root_key`、`root_sha256`、`root_size`）的定长 list、`manifest_json`（v3 manifest 本身固定大小）。
  - 新 selection chunk 表（§4.1）。[DQ-2][DQ-3]
- 新表按 C3 / D3B / E2 / QG-1 / DS-1 先例以 additive 方式追加进 `PHASE1_TABLES`，前 15 张定义与哈希不变；golden 测试按先例追加。表名在 Codex 决定前不冻结。

### 8. 旧 v2 manifest 的只读重放（硬要求）

1. `ResearchDatasetManifest` 模型、`research.dataset_manifests` 与 `research.dataset_selections` 的定义、`hlens.dataset.pit-selection@1.0.0` 规则常量（`DATASET_RULE_SPEC` / `DATASET_RULE_HASH`）与 v2 `selection_id_for` 保持逐位不变，保留为 legacy 代码路径。
2. `ManifestStore.load(hash)` 按内容哈希在两张 manifest 表中查找：只在 v2 表 → v2 路径（现有 `manifest_row` 等值、JSON 哈希、`verify_manifest` 全量重导出，在记录版本作用域内）；只在 v3 表 → v3 路径；两表都有 = `CatalogIntegrityError`。
3. v2 路径**明确标注为物化式**：其验证工作集随 N 增长，不属于 E1-CAP-1 的有界声明，也不被本 ADR 改造。
4. 推荐：本 ADR 实施后**不再新建** v2 manifest；v2 规则下已提交但缺 manifest 的 dataset 批次保持不可用（ADR-0023 §6），不在新代码中补写 v2 manifest；需要该数据集时以 v3 规则重建（不同规则 → 不同 `selection_id`，互不混淆）。[DQ-10]
5. v2 manifest 不迁移、不重写、不改哈希；读取 v2 不赋予 v3 字段，也不"升级"为 v3。

### 9. 失败与孤儿对象语义

| 失败点 | 结果 | 恢复 |
|---|---|---|
| evidence 叶 / 索引对象已发布，构建随后失败 | 内容寻址的 orphan 对象；无 manifest 引用，任何读者不可用 | 重跑幂等（`already_present`）；清理只由未来显式 maintenance，写路径不删除 |
| 部分 chunk 已提交，构建失败 | 无 manifest 的 chunk 批次，不可进入实验 | 重跑核对已提交前缀后续写（§4.4） |
| 已提交 chunk 与重新派生不一致、chunk 空洞、重复 chunk 批次 | `CatalogIntegrityError`，不提交后续 chunk 与 manifest | 人工调查；不覆盖 |
| 单条记录超过 `leaf_max_bytes` | fail closed，无 manifest | 须新规则版本调整参数 |
| manifest 行写入竞争 | 同内容哈希幂等；不同内容同哈希或两行 = 完整性错误（同 v2） | 重读后重试 |
| 对象缺失、字节被改、索引截断 / 重排 | `iter_evidence` / verifier 抛错；`load` 不返回 manifest | — |
| PIT conflict、未登记 binding、缺报告、缺口未记录 | 同 v2，fail closed | 同 v2 |

### 10. 资源边界与 E1-CAP-1

本 ADR 的实现目标是：**v3 Dataset 构建、验证与读取的仓库侧持有量有显式固定上界**；输入行数、窗口天数、成员 / 排除数、lineage 数与报告数只影响运行时间、持久化对象数和 Iceberg snapshot 数，不得使进程保留相应大小的集合。§6.1 所列上游流是此目标的一部分，不能延期。

这不等于容量验收已通过。以下字节级实现边界仍须在开发收敛后由 E1-CAP-1 统一测量并满足 32 MiB 门槛：PyIceberg metadata / Avro manifest bytes、Arrow row group / ORC stripe、固定排序 run、evidence 叶与索引缓冲、reporter 单报告生成路径、Iceberg snapshot history 增长，以及 OS 与 Python allocator 的额外驻留。系统默认临时目录不得进入 v3 路径。未达到硬界或不能证明磁盘 / 内存策略时，E1-CAP-1 继续阻断；ADR 实施和静态复核不构成 E1-CAP-1 或 Phase 1 通过证据。

## 契约版本策略比较

| 维度 | **A. 2.3.0 additive（推荐）** | B. 全局 major 3.0.0 | C. 不进 core，只在 infrastructure 定义 v3 |
|---|---|---|---|
| 做法 | 新增模型（`_MODEL_SINCE = "2.3.0"`），`CONTRACT_SCHEMA_VERSION = "2.3.0"`，`PUBLISHED_CONTRACT_SCHEMA_VERSIONS` 追加；`ResearchDatasetManifest` 不变 | 原地改写 `ResearchDatasetManifest`（或删除 v2 形态），全局升 major | v3 manifest 为 infrastructure DTO |
| 已发布契约 | 不变；符合"minor 只加新模型 / 可选字段" | 所有模型的 major 改变；`_supported_major` 只收 3，2.x 须走新的 `core/compat/v2` 只读入口 | 不变 |
| 已持久化数据 | 按 ADR-0052 V1～V7 记录版本重放；2.x 行同表并存 | Phase 1 全部表的 2.x 行、全部 `PolicyBinding` / `SourceBinding` / 登记 universe（2.0.0 信封）、D-NET 数据、v2 manifest 都需 v2 兼容读取与重放设计；ADR-0052 方案 A 因同样代价被拒 | 不变 |
| v2 manifest 只读兼容 | 天然满足（同一模型、同一 major） | 需完整 v2 compat 层；按 ADR-0010 的 v1 先例，compat 记录不是 `Contract`，**无法**再用现有 `verify_manifest` 以模型重导出——与硬要求冲突，除非另建 v2 模型副本 | 天然满足 |
| 新对象哈希 | 当前版本新建对象信封变为 2.3.0，内容哈希随之改变（minor 的预期后果，02-domain §3.3）；须按 ADR-0052 §4 先盘点 code-registered 身份与重放路径 | 全部改变 | 不变 |
| 语义 | ADR-0023 §6 的"manifest 绑定成员 / 排除 / lineage"由"内联"扩为"内联或经内容哈希承诺的外部有序流"；需同步 03-data §3 / §7.5 文档 | 同左，另加全局迁移 | Runner / research 无法在 core 中类型化消费 v3 manifest；与 ADR-0023 §6"manifest 是契约"不一致 |
| 风险 | 版本升级对 Phase 1 重放路径的影响须测试证明（ADR-0052 M1/M2 已建立机制） | 爆炸半径覆盖全项目；与 ADR-0023 H、ADR-0052 A 的既有裁决方向相反 | 契约义务落空 |

推荐 A。B 只有在未来另有与此无关、必须破坏已发布字段的变化时，才应与之合并考虑；本 ADR 不单独触发 major。

## 决策问题（Decision Questions，均待 Codex 复核决定）

| ID | 问题 | 选项 | 推荐 |
|---|---|---|---|
| DQ-1 | 契约版本策略 | A 2.3.0 additive 新模型；B 全局 3.0.0；C infrastructure-only | A |
| DQ-2 | v3 manifest 存哪 | a 新表（固定大小列）；b 既有表 schema evolution + 空 `quality_report_ids`；c 既有表加判别列、`manifest_json` 存 v3 | a（b / c 使 `quality_report_ids` 语义失真，且危及 v2 等值重放） |
| DQ-3 | selection 行的 ordinal | a 新 chunk 表含 `chunk_index` / `row_ordinal`；b 既有表加可选列；c 不加 ordinal，按 chunk snapshot 新增文件 + 自然键排序 | a（b 改冻结定义并破坏 v2 指纹重放；c 依赖 snapshot 文件级读取原语，且无全局序）。分区另议：沿用 `symbol + day(event_time)` 为推荐 |
| DQ-4 | 行与 lineage 的规范顺序 | a 生成顺序 + 显式 ordinal；b 沿用 v2 全局 `_row_order` | a（b 需外部排序，工作集或临时文件随 N） |
| DQ-5 | 跨 slice key 唯一性证明 | a 由 PIT 的 key ownership 规则独立复核，并按有序流检查相邻 slice；b O(N) `seen_keys`；c Iceberg 中的 key 索引表 | a（需由 v3 selector verifier 逐项重证；b 被禁止，c 无必要） |
| DQ-6 | 对象键 | a 仅按内容哈希 `research/dataset-evidence/v1/<sha256>.jsonl`；b 含 `selection_id` 前缀 | a（幂等、去重；b 便于按数据集维护但同内容多份） |
| DQ-7 | 流内记录的信封 | a 记录行不含 `schema_version`，由 manifest 记录版本在作用域内重建；b 每行完整 `model_dump` 含信封 | a（避免每行重复信封；与 ADR-0052 V1 一个写入组一个版本一致） |
| DQ-8 | manifest 中对象引用 | a `key + sha256 + size`，不含 `uri`；b 完整 `ObjectRef` | a（`uri` 由实现生成，纳入哈希会使同内容在不同 warehouse 根下身份不同） |
| DQ-9 | `chunk_rows` / `leaf_max_records` / `leaf_max_bytes` / `fanout` 数值 | 由实现批次在容量测量后提出，写入规则 spec | 本 ADR 不选数值；取值须同时考虑 32 MiB 与 snapshot 数增长 |
| DQ-10 | v2 规则的后续写入 | a 实施后不再新建 v2，缺 manifest 的 v2 批次不补写；b 允许为既有 v2 批次补写 v2 manifest | a（b 需保留 O(N) 写路径，且补写对象不在有界声明内） |
| DQ-11 | 对象哈希先于 staging 的获得方式 | a 有界内存组装单个对象后哈希再 stage；b 扩展 `StorageAdapter`（先写后哈希）；c 整流 spool 到临时文件 | a（b 改 Data Plane Protocol，需另立 ADR；c 临时文件计入工作集且可能在 tmpfs） |
| DQ-12 | 空选择 | a 维持拒绝；b 允许零 chunk 的 v3 manifest | a（无自身 snapshot 可绑定，维持 ADR-0023 §6 与 v2 语义） |

## 决定记录（2026-09-28）

Raphael 已批准有界 Dataset API 的路线及 v2 manifest 只读兼容要求。Codex 依 Raphael 2026-09-28 授权审阅本草案后，决定如下：

| ID | 决定 / 状态 | 依据与边界 |
|---|---|---|
| DQ-1 | 选择 A：新增 `2.3.0` additive core model（**Raphael 2026-09-28 亲自批准**） | v2 模型原样保留最符合兼容要求；授权范围仅限在 `core/contracts` 新增模型、版本登记与同步冻结契约文档，不改任何已有模型。 |
| DQ-2 | 选择 a：新建定长 v3 manifest 表 | 不改 v2 manifest 行与 `quality_report_ids` 语义。 |
| DQ-3 | 选择 a：新建 chunk 表并包含 `chunk_index` / `row_ordinal` | 保持既有 selection 表 schema、分区和重放指纹不变。 |
| DQ-4 | 选择 a：按生成顺序写入并记录 ordinal | 允许定长 chunk 写入和逐块核验。 |
| DQ-5 | 选择 a：由 PIT key ownership 规则复核唯一性 | 重复与跨 slice 冲突仍 fail closed，不保留 O(N) seen set。 |
| DQ-6 | 选择 a：对象键仅由内容 SHA-256 派生 | 内容寻址、幂等写入。 |
| DQ-7 | 选择 a：evidence 行不重复写 `schema_version`，由 manifest 记录版本作用域重建 | 遵守 ADR-0052 记录版本重放规则。 |
| DQ-8 | 选择 a：manifest 只存 `key + sha256 + size` | `uri` 是部署位置，不进入内容身份。 |
| DQ-9 | **OPEN**：chunk / leaf / fanout 数值 | 仅可在容量测量后选择；本轮禁止运行探针，且 32 MiB 门槛不变。 |
| DQ-10 | 选择 a：新路径停止创建 v2；不补写缺 manifest 的旧 v2 批次 | 旧 v2 继续只读兼容；新构建按 v3 规则产生不同 selection identity。 |
| DQ-11 | 选择 a：在固定对象缓冲内组装、哈希后 staging | 不改 `StorageAdapter` 契约；超限对象 fail closed。 |
| DQ-12 | 选择 a：空选择继续拒绝 | 无可绑定的 selection snapshot。 |

~~本 ADR 状态为 **BLOCKED**~~（已解除，2026-09-28）：Raphael 亲自批准 DQ-1 = A，本 ADR 转为 **Accepted**。
这不代表 DQ-2～DQ-12 已进入生产实现：本轮只实现契约层（见「实施记录：契约层」）；infrastructure 层（新表、evidence
writer / reader、chunk commit、streaming verifier、`ManifestStore` 双表分派、上游生成器）仍未实施，现有 Dataset 运行
行为不变。infrastructure 实施前仍须完成 §6.1 / §6.2 所列上游生成器、partial replay、序列化投影和消费者可见性协议的
静态设计（§6.2.2 的 2.3.0 登记身份盘点已在契约层完成，见实施记录）；DQ-9 保持 OPEN。

### 实施决策：Universe 有界上游接线（2026-09-29，Codex）

依据本 ADR §6.1.1 与 §10，采用 content-addressed run-set 方案收口 v3 Universe 上游；这是已接受协议的实现细化，不改变契约、DQ-9 数值或 v2 行为：

1. `dataset_evidence_sources()` 显式把既有 `UniverseRunParams` 传入 `UniverseBuilder.cursor()`；DQ-9 参数仍由调用方提供，不新增默认值。
2. 扩展 `infrastructure/pit/runs.py`，提供内容寻址、层次化 run-reference set 与多轮有界归并。Universe 变化事件、lineage 和 gap 归并只保留当前容量批、最多 `merge_fanout` 个 run 及树层缓冲；不得收集全部 run refs。
3. `_instants_v3()` 将 cutoff 可见的 exchange-info / listing boundary 流写入有界 sorted runs，按时间归并并相邻去重后供各 symbol 重放；不构造全窗口 `changes` set / tuple。
4. `_events_v3()` 不保留全量 revision `seen` set。事件为 lineage / gap 附加确定性首次次序，经 revision 排序后相邻归并只输出首次项；相同 revision 的冲突内容 fail closed。
5. 所有 run 通过现有 `StorageAdapter` 内容寻址对象路径发布，不使用 `tempfile` / `TMPDIR`，不新增 Universe scratch 配置。对象失败语义沿用 §9：允许留下不可引用 orphan，不在写路径删除。
6. 改动范围包含 `infrastructure/universe/builder.py`、`infrastructure/dataset/sources.py`、`infrastructure/pit/runs.py` 与相关测试；保持 cutoff、排序、首次 lineage、重复拒绝和关闭语义。DQ-9 数值与 E1-CAP-1 仍须后续测量和验收。

**实施 / 独立复核记录（2026-09-29）**：隔离候选提交 `7442bf66f14afdf5667ea1fc2a87de3f62eb2b0f` 完成以上接线。不同 agent 对精确提交独立复核为 APPROVE。开发 focused suite 为 `82 passed in 80.41s`；复核测试覆盖合计 `118 passed`，含核心 RunSet/Universe/Dataset/red-team、bars/feature/G5 调用链和旧 Dataset universe consumer。Ruff、format、mypy（4 个生产文件）与 `git diff --check` 通过，候选工作树干净。此结果仅证明本实现切片，不证明完整 32 MiB 工作集容量；E1-CAP-1、DQ-9 数值和 Phase 1 仍开放。

**后续 PIT RunSet 复核记录（2026-09-29）**：候选 `972c6c7980352ea546ff65a1914f18cf7e6a1733` 将 `PitSelector.iter_bounded()` 的 row/edge run-ref 列表改为层次化 root；test-only follow-up `6a80b67030105420fe8f33306f59e14757e6ebc2` 直接计数 compaction 与两个 root readers。独立 reviewer APPROVE；PIT selector + Dataset v3 source `41 passed`，Ruff、format、mypy 和 diff-check 通过。单 key 的 rows/edges/availability/graph 物化及 `maximal_heads` 无界 tuple 仍未解决，不能据此声称 PIT 或 E1 有界。

### Raphael Decision Packet（DQ-1）——已决定：A（Raphael，2026-09-28）

- **问题：** 是否批准新增契约 2.3.0 的有界 Dataset manifest 模型，并修改其必要的 `core/contracts/`、版本登记及冻结契约文档？
- **A（推荐）：** 批准上述 additive minor 变更；旧 v2 manifest 保持原样只读。影响是冻结契约和契约版本登记发生受控变更，须另立已授权实施批次。
- **B：** 不修改 core；保留现行 Dataset DTO / manifest 的物化行为，并将该路径标记为不满足有界工作集要求。
- **阻塞：** DQ-1 决定前不实施 ADR-0077 的新 Dataset API、表或持久化路径。

## 实施记录：契约层（2026-09-28，W3 任务 A1-E1DS-CONTRACT；未运行任何测试 / lint / typecheck）

实施者 Claude Code（Opus），只在 DQ-1 授权范围内新增，不改任何已有模型；未运行 pytest / ruff / mypy / Schema 导出
（本阶段 WSL 内存受限，按协调要求不跑检查），以下全部为静态自检，须由复核方真实运行后才可接受。

1. **契约版本**：`CONTRACT_SCHEMA_VERSION = "2.3.0"`，`PUBLISHED_CONTRACT_SCHEMA_VERSIONS` 追加 `"2.3.0"`（`core/domain/base.py`）。
   当前代码新建、未显式给出信封的对象取 2.3.0，内容哈希与 2.2.0 孪生对象不同（minor 的预期后果，02-domain §3.3）。
2. **新模型**（`core/contracts/universe.py`，全部 `_MODEL_SINCE = ADR_0077_VERSION = "2.3.0"`，登记在 `CONTRACT_MODELS` 末尾，
   135 → 141）：`DatasetRuleBinding`、`EvidenceObjectRef`、`EvidenceStreamRef`、`DatasetQualityReportRef`、`DatasetChunkProof`、
   `ResearchDatasetEvidenceManifest`，以及枚举 `EvidenceStream` / `DatasetQualitySubject`、常量 `DATASET_EVIDENCE_FORMAT`、
   `DATASET_EVIDENCE_KEY_PREFIX` / `_PATTERN`、`DATASET_SELECTION_ID_PATTERN`、`DATASET_CHUNK_INDEX_MAX` 与纯函数
   `dataset_evidence_key(sha256)`、`dataset_chunk_batch_id(selection_id, chunk_index)`。契约层不变量按 §1.5 落地（见 02-domain §2.3）；
   `selection_id` 的派生公式、`first_row_ordinal = chunk_index × chunk_rows`、fan-out 与 depth 的精确关系留给 verifier。
3. **Schema**：`schemas/` 135 份既有 Schema 只把信封默认值 `2.2.0` 改为 `2.3.0`（逐文件核对：映射回 2.2.0 后与改动前字节相同）；
   新增 6 份 Schema 为**手工编写**，须由复核方运行 `python -m core.contracts.registry` 重新导出并 diff，任何差异以导出为准。
4. **v2 不变**：`ResearchDatasetManifest` 及其记录模型的字段、校验、Schema 与内容哈希不变；`research.dataset_manifests` /
   `research.dataset_selections`、`DATASET_RULE_SPEC` / `DATASET_RULE_HASH`、`selection_id_for` 未触及（infrastructure 本轮未改）。
5. **§6.2.2 的 2.3.0 升版盘点（ADR-0052 §4 同等要求）**，结论：**没有**随默认信封漂移且被持久化数据按内容引用、又无重放机制覆盖的已登记身份。
   - Phase 1 代码登记身份（V3）：`infrastructure/` 中全部 16 个模块级 `PolicyBinding` 常量、3 个 `SourceBinding` 常量
     （archive / REST / exchangeInfo）与 `FIRST_SLICE_UNIVERSE` 均显式 `schema_version=PHASE1_PUBLICATION_VERSION`（2.0.0）；
     `UniverseSpecBinding` 由 `binding()` 继承 spec 信封（V4）。
   - 从行 / 对象重建的绑定（`listing_rules`、`revision/store`、`channel_reconcile`、`row_integrity`）按记录版本或在
     `contract_schema_version_scope` 内构造（V1）；`event.events` 逐行记录运行版本；dataset manifest 按记录版本复核（V7）。
     这些路径只要求记录版本属于 `PUBLISHED_CONTRACT_SCHEMA_VERSIONS`，2.0.0 ~ 2.2.0 仍在其中。
   - 规则哈希（`DATASET_RULE_HASH`、`NORMALIZER_HASH` 等）是规则 spec 字典的规范 JSON 哈希，不含契约信封（V5）。
   - 知识种子 `docs/research/knowledge/*.json` 显式记录 `schema_version: "2.1.0"`（ADR-0055）。
   - 研究侧：P7 durable loop 恢复时用当前代码构造的种子策略 spec / risk policy 的内容哈希与审计行比对（`research/loop/durable.py`），
     没有按记录版本重建；与 2.1.0 / 2.2.0 两次升版的既有判断相同，仓库外没有需要跨升版恢复的真实 durable 状态，因此不构成阻断，
     但**跨 minor 升版恢复一个既有 durable loop 会 fail closed**（记为风险，不在本 ADR 范围内修改）。
6. **仍 OPEN / 不在契约层**：§6.2.3 evidence JSONL 的唯一序列化投影（DQ-7 = a：记录行不含信封，按 manifest 记录版本作用域重建）
   须由 infrastructure 批次在 writer / reader 中以单一函数固定；§6.1 上游生成器、§6.2.1 / .2 / .4 / .5 / .6 协议、两张新表的冻结、
   DQ-9 数值均未实施。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未推荐 |
|---|---|---|---|
| 保持 v2 冻结 API，记录 Dataset 为物化路径（决策包方案 A） | 零迁移 | 不满足有界工作集 | Raphael 已选方案 B |
| 只把 `seen_keys` / lineage 字典 spill 到 SQLite / 临时文件 | 改动小 | 返回对象与 manifest 身份仍 O(N)；临时文件计入工作集，可能在 tmpfs | 决策包明确排除"局部 spill 冒充有界" |
| manifest 内联证据但分页成多行（同一 manifest 多行） | 仍在 Iceberg 内 | 内容哈希仍需整份序列化；读取需 per-manifest 全量扫描排序 | 身份定义未解决 |
| 证据写成 Iceberg 表而非对象 | 查询方便 | 每条流一批或多批提交，snapshot 增长更多；有序读取仍需 ordinal 与排序 | 对象 + 承诺树的读取路径更简单、完全确定 |
| 单一整流对象 + 整流哈希（非树） | 结构最简单 | 需先知道整流 SHA-256 才能 `stage`，只能 spool 或两遍生成；读取时无法在中途认证 | 与 `StorageAdapter` 语义和固定内存目标冲突 |
| 以 `add_files` 一次提交所有 chunk 的 Parquet 文件 | snapshot 少 | 不在现有 adapter 契约；ADR-0023 §7 已选 microbatch | 需另立 ADR |

## 后果（Consequences）

- 正面：按 §6.1 实施后，v3 Dataset 构建 / 验证 / 读取不在仓库代码中持有 O(N) 或 O(窗口天数) 集合；v3 manifest 固定大小，仍以内容哈希承诺全部成员、排除、lineage、报告、缺口与每个 chunk；验证强度不降低；v2 数据逐位可读可验证。
- 负面 / 代价：构建与验证各需一次完整重新派生（CPU / IO 翻倍量级）；每个 dataset 产生 `chunk_count` 个 snapshot；新增两张表、若干契约模型、一种 evidence 对象格式与相应 maintenance 义务；v2 与 v3 两条路径长期并存；infrastructure API 调用方须迁移到摘要 + 显式迭代器。
- 需要迁移的内容（接受后的实施批次）：`core/contracts/universe.py` 新模型与 Schema 导出；`core/domain/base.py` 版本常量；`infrastructure/catalog/phase1_tables.py` 两张新表；`infrastructure/dataset/`（builder 生成器、chunk 写入、evidence writer / reader、verifier、`ManifestStore` 双表分派）；消费者（`infrastructure/tools/`、研究侧数据集读取）改用摘要与迭代器；文档 `03-data.md` §3 / §7.1 / §7.5、`02-domain.md` §2.3 / §3.3、ADR-0033 补注。
- 对复现性的影响：v2 数据集按原规则、原哈希、原 snapshot 复现；v3 数据集由（规则版本 + 参数 + PIT spec + universe + data_type + 窗口 + 绑定 snapshot）唯一确定，构建与重放按位一致。v2 与 v3 对同一输入产生不同 `selection_id` 与 manifest，二者不可互换或比较哈希。

## 实施前验收矩阵（实施批次执行；本 ADR 未运行）

1. v2 golden：既有 v2 manifest（含 2.0.0 / 2.1.0 / 2.2.0 信封）在新代码下 `load` / `verify_manifest` 逐位不变。
2. v3 同输入两次构建按位相同；中途在任意 chunk / 任意对象发布后崩溃，重跑完成且结果与一次成功构建相同。
3. 篡改：删 / 改 / 重排任一叶或索引对象、删 / 改任一 chunk 行、插入额外 chunk 批次、制造 chunk 空洞、同哈希两表各一行——全部 fail closed。
4. 语义回归：成员 / 排除冲突、listing revision 多 episode 认领、缺 listing lineage、缺口未被报告记录、跨 slice key 重复——与 v2 相同地拒绝。
5. 超长记录、空选择、未登记规则 / 参数 —— 拒绝。
6. 2.3.0 升级的重放盘点（ADR-0052 §4 同等要求）：Phase 1 各写入组在升级后重放幂等、同表混版可读。
7. 资源：在 E1-CAP-1 口径下独立测量，本矩阵的通过不等于容量通过。

## 合规检查

- [x] 不破坏已冻结契约，或已说明 major 版本与迁移路径——DQ-1 = A（Raphael 2026-09-28 批准）：additive minor 2.3.0，已发布模型不变
- [x] 不修改 Validation Constitution / Profile / 验证规则 / E1-CAP-1 门槛；动机不是让某实验通过
- [x] Domain 层仍无具体技术依赖（新模型只用标准库与 Pydantic；对象存取经 `StorageAdapter`）
- [x] Research / Application Plane 边界不变
- [x] 不删除历史：v2 表、行、manifest 保留；orphan 只由显式 maintenance 处理
- [x] 本 ADR 起草时未修改任何代码、契约或其它文档（`docs/adr/README.md` 索引行除外）；契约层实施的改动范围见「实施记录：契约层」

## 参考

- [数据集有界结果决策包](../reviews/2026-09-28-dataset-bounded-result-decision.md)
- [E1-CAP-1 对账](../reviews/2026-09-28-e1-cap1-design-reconciliation.md)
- `core/contracts/universe.py`（`ResearchDatasetManifest` 及记录模型）、`core/contracts/storage.py`（`StorageAdapter`）、`core/contracts/catalog.py`（`BATCH_ID_PATTERN`）、`core/domain/base.py`（版本常量、`_MODEL_SINCE`、`contract_schema_version_scope`）
- `infrastructure/dataset/builder.py`、`manifests.py`、`selection.py`；`infrastructure/catalog/phase1_tables.py`（`DATASET_MANIFESTS`、`DATASET_SELECTIONS`）
- `docs/architecture/02-domain.md` §3.3、`docs/architecture/03-data.md` §3 / §7.1 / §7.5

### PM 冻结记录：新表（2026-09-28，Claude PM 依 Raphael 授权）

按 §6.2.5，冻结 B-TABLES 实现的两张新表（`infrastructure/catalog/phase1_tables.py` 第 16、17 项）：

- `research.dataset_evidence_manifests`：不分区，列与 `ResearchDatasetEvidenceManifest` / `EvidenceStreamRef` / `EvidenceObjectRef` / `DatasetRuleBinding` 一一对应。
- `research.dataset_selection_chunks`：v2 `research.dataset_selections` 的 8 列（字段 ID 1–8）完全保留，另加 `chunk_index` 与 `row_ordinal`；分区为 `identity(symbol) + day(event_time)`，与 v2 相同。

两张表的 definition hash 与 golden 字面值在调试阶段首次运行时钉定。已有 15 张表的定义与哈希不变。

### PM 决定：B-BUILD 遗留项（2026-09-28，Claude PM 依 Raphael 授权）

1. **listing lineage 顺序**：保持 ADR §2 的规定，按 `canonical_revision_id` 排序。B-UNIV 的 `listing_lineage()` 按生成顺序产出，因此经 `infrastructure/pit/runs.py` 的内容寻址有序 run 重排后再交给 builder。builder 的顺序校验保留，顺序不符即 fail closed。
2. **按 symbol 持有 member spans**：允许。持有量的上界是该 symbol 在窗口内的成员变更次数，与 B-UNIV instants 的上界相同，不随行数增长。§6.1 所禁止的是跨 symbol 或随行数增长的全量映射。
3. **按报告缓存 quality gaps**（§6.1.5）：允许，上界为单份报告的缺口数。
4. **窗口外、到达晚于后续日报告的 event day**：fail closed。
5. **DQ-9 规则参数登记**：仍为 OPEN，待调试阶段的容量证据。
6. **header 与引用行的 512 字节上限**：属于 evidence 文件格式常量，不是 DQ-9 调优参数。
