# infrastructure/

部署与运行时配置：容器、Compose、可观测性、Adapter 的基础设施侧配置（08-deployment.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `settings.py` | 类型化运行时设置（03-data.md §6.2）：`file://` warehouse / staging、PostgreSQL catalog DSN、HTTP / Binance base URL |
| `storage/` | Phase 1 C1 本地 `file://` `StorageAdapter`（`LocalFileStorageAdapter`） |
| `catalog/` | Phase 1 C2 PostgreSQL-backed PyIceberg `CatalogAdapter[pyarrow.Table]`（`PyIcebergCatalogAdapter`）与定义登记表；C3 八张首切片生产表 + D3B 四张 ADR-0027 REST 表 + E2 一张 ADR-0029 快照表 + QG-1 一张 ADR-0031 证据缺口表 + DS-1 一张 ADR-0033 Research Dataset 选择表（共 15 张）、batch 指纹规则与 partition-spec 演进 |
| `collector/` | Phase 1 D0 Binance 公共现货日归档下载壳（`BinanceSpotArchiveCollector`） |
| `parser/` | Phase 1 D1 Binance 公共现货日归档 fail-closed parser（`binance.spot.archive.parser@1.0.0`） |
| `revision/` | Phase 1 D2 append-only Raw revision：身份规则、availability / precedence policy、`RawRevisionStore`；D3B 的 REST 纯规则（独立身份规则、REST availability / precedence、D-33 通道等价比较） |
| `canonical/` | Phase 1 E1（ADR-0028）：四个 Canonical 规则（身份 `crev1-`、normalizer、派生 availability、precedence-map 纯函数）与 `CanonicalNormalizer`——一个 Raw source revision 为一个单元，先经 `PersistedRowVerifier` 证明全部 Raw 行，再一一映射为 `canonical.trades` / `canonical.bars_1m` 行；独立 `arrival_seq` 块、一次时钟读数、崩溃恢复复用（批次号记录单元行数与批大小，续跑与核对只按已提交批次号的计划，E1-R3）；每次调用固定三张表的快照、按批次窗口证明与写入，内存随批大小而非单元大小增长（G3-S）；不读 Raw 证据表；E4 `resample.py`（`hlens.canonical.resample@1.0.0`）：只从 F1 选中的 1m bar 派生能整除一天的周期，UTC 对齐，不补缺，精确十进制运算（不精确即拒绝），`available_time` 不早于区间结束 |
| `pit/` | Phase 1 F1：`PinnedCatalogView`（按 manifest 绑定的 snapshot 只读；未绑定的表读作空）与 `PitSelector`（规则 `hlens.pit.maximal-head@1.0.0`：绑定核对 → 在绑定 snapshot 上证明 Canonical 行（只证明读到的批次，单元级事实全部核对，G3-S2）与 Raw 边；窗口为任意 UTC 区间，成交数据按小时分段 → 映射边 → 双截止 maximal-head；输出选择、lineage、证据缺口、冲突）。`PyIcebergCatalogAdapter.scan_columns` 为此增加可选 `snapshot_id`（仅基础设施层，核心 Protocol 不变） |
| `quality/` | Phase 1 E3：`QualityReporter`（规则集 `hlens.quality.canonical-partition@1.0.0`）——每个 Canonical 分区（表 × 标的 × UTC 日）一行 `quality.data_quality_reports`：在当时各输入表的固定快照上经 F1 证明后生成事件（输入绑定、竞争 head、1m 缺口、aggTrade ID 跳号、K 线不变式违例）与全部证据缺口；report_id 由输入快照决定，重跑复用首次时间；成交按小时分段证明、只处理有数据的时段，同一报告内每个单元只证明一次（G3-S3；证据缺口格式待 D-QGAP）。**不含任何数值阈值**（异常值检测需校准，留待后续规则版本）；F3 增加 `existing_only`（只复算已提交报告）。`listing_report.py`：`ListingQualityReporter`（`hlens.quality.listing-history@1.0.0`）——listing 历史一行报告：输入快照、E2 推导发现（未知状态、缺 symbol、分歧等）为质量事件，全部 listing revision 的证据缺口 |
| `universe/` | Phase 1 F2（ADR-0024 / 0029）：`UniverseBuilder`——已登记的 `UniverseSelectionSpec`（首切片 `binance.spot.btc-eth@1.0.0`，spec 中为 venue 原生 symbol，候选须是由该 symbol 观察推导、instrument 为其 Canonical 形式的 episode）在 PIT 规格绑定的 snapshot 上经 E2 `ListingDeriver.listing_at` 求成员 / 排除（暂停 = `not_tradable`）；区间在起点与区间内每个可能变化的时刻求值并合并；缺 listing 绑定 / 政策、未登记 spec、任何 unconstructible（含首次本机观察之前）一律 fail closed；带 filter 的 spec 因无 PIT 指标输入而拒绝（不设阈值） |
| `dataset/` | Phase 1 F3：`DatasetBuilder`（规则 `hlens.dataset.pit-selection@1.0.0`）——绑定全部已登记、证据表有快照即须绑定 → F2 universe → 每个成员按切片（成交按小时、K 线按天）`PitSelector` 选择、`require_no_conflict`、按成员区间门控 → 每个覆盖分区与 listing 历史须已有**恰在绑定 snapshot 上**的质量报告（`existing_only` 复算，不写、不读时钟），缺口经 `evidence_gaps_of` 绑定 → 一批写入调用方给定的 research 表（批次号 = `selection_id`，重建重放同一 snapshot；空选择拒绝）→ `ManifestStore` 以内容哈希幂等写 `research.dataset_manifests` 并复核。research 表的形状仍在 `selection.py`（`SELECTION_SCHEMA`）——DS-1（ADR-0033）把它登记为生产表 `research.dataset_selections`（`catalog/phase1_tables.py` 第 15 张），`selection.py` 反向导入其 schema 作为唯一来源 |
| `feature/` | Phase 1 F4（ADR-0030）：`run_feature`——对每个评估时刻只把可见集合（`available_time + available_lag <= t`，同键取最晚可用，`knowledge_time <= knowledge_cutoff`）交给 `FeatureProvider`，每个回答经 `FeatureResult.check_answers` 核对；`bar_observations` / `derived_bar_observations` 从 F1 选择（无冲突）与 E4 完整派生 bar 构造带 lineage 的 `FeatureObservation`，`pit_feature_request` 拒绝 PIT 视图覆盖不到的评估时刻（`t - lag` 须在 simulation 区间内）；`dataset.py`（G2-R1c/R1-I/R2）：`feature_request_from_dataset`——只经调用方的 `DatasetBuilder` 自身验证存储加载 `ResearchDatasetManifest`（RT-6 修复），在清单绑定的快照与 spec 下重新证明观测，是唯一声明"这次特征运行绑定了哪个数据集"的入口；不 import 任何插件 |
| `state/` | Phase 2（ADR-0035；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`run_state`——对每个评估时刻只把可见 Feature 值（`evaluation_time <= t`，训练型再限于 `(t - training_window, t]`）交给 `StateProvider`，每个回答经 `StateResult.check_answers` 核对，训练型规格须固定 `seed`、输入须属于 `StateSpec.features`；`state_inputs` 只从已回答的 `(FeatureRequest, FeatureResult)` 构造输入；`state_table` 把一份结果物化为 Arrow 表（未写入 catalog）；不 import 任何插件 |
| `strategy/` | Phase 5 接线（ADR-0038 实现注记；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`signals_from_features` / `signals_from_states` / `signals_from_events` 把已回答的 F4 / Phase 2 / Phase 3 结果转成 `SignalObservation`；评估于 `t`（或在 `t` 可观测）的值在 `t` 可用，知识时间由调用方显式给出（上游运行的知识截止），`None` 原样保留，Outcome 永不成为信号 |
| `event/` | Phase 3（ADR-0036）：`run_events`——对每个检查点只把可见集合（`available_time + observable_lag <= t`）交给 `EventProvider`，每个回答经 `EventResult.check_answers` 核对（事件时间 = 可观测时间、引用可追溯），相邻检查点的事件表必须一致（回填 / 撤回 = 未来确认，fail closed）；`inputs.py`：`inputs_from_feature_run`（逐点因果 lineage `feature_value_lineage`）与 Phase 3 本地 State 序列形状 `inputs_from_state_series`；`state_series_from_state_run` / `state_value_lineage` 是 Phase 2 → Phase 3 的接线适配器（ADR-0035 §1 / ADR-0036 §1）：把一次 `(StateRequest, StateResult)`（经 `infrastructure.state.run_state`）转成 `StateSeriesPoint`，lineage 只绑定 state 身份与该点的 `StateValue`本身，从不绑定覆盖全部评估时刻的 `request_hash` / `result_hash`，因此未来数据不改变过去的点或引用它们的事件的身份；`table.py`：`event_table` 逻辑物化（物理表未登记）；不 import 任何插件 |
| `tools/` | Phase 1 G3-T/G3-C：`capacity_probe.py`——只 import 生产模块的运维容量探针，在临时 SQLite catalog + 本地 warehouse 上合成数据跑归档/REST/E1/F1/E3 四段基线，`--rest`/`--dataset`/`--feature` 三个可选阶段串起 D3D～D3E、E2、F2/F3、F4；量的是执行耗时与内存峰值，不是正确性证据；不 import `tests/` |
| `plugins/features/`（不在本目录下，见 `plugins/README.md`） | Phase 1 F4（ADR-0030）唯一现有插件：`feature/` 契约的三个首批 `FeatureProvider` 实现（`BarLogReturnProvider` 等），只依赖 `core`，在此列出便于按 Phase 1 批次索引 |

## LocalFileStorageAdapter（C1 / C1-R1 / C1-R2 / C1-R3）

- 只接受 settings（或等价的本地绝对 `file://` URI）给出的 warehouse / staging 根；两者必须在同一文件系统，且不得为同一路径（允许 staging 为 warehouse 的严格子目录或同文件系统上的独立目录）。
- 构造时记录 warehouse / staging 根 inode；**每次操作**临时打开根 FD 并核对仍绑定构造时的 inode，再以 `dir_fd` + `O_NOFOLLOW` 逐段访问，拒绝路径组件或最终对象上的 symlink。实例不跨操作持有根 FD；提供幂等 `close()` / context manager，关闭后操作失败。
- 调用方只传 B3 逻辑 object key；实现在每个入口复核 key，并拒绝映射进 staging 私有区的 key。
- `stage`：有界流式写入 staging；可靠 write-all（短写 / `InterruptedError` / 返回 0）；边写边算 SHA-256 与字节数；校验或写故障清理**本次**临时文件，不产生可见对象。
- `publish`：以已打开的 staging entry FD 与最终父目录 FD 为锚点做同文件系统 `os.link`（不覆盖；同内容幂等，异内容 `ObjectConflict`）；link 后从**实际最终对象 FD**重验普通文件 / SHA-256 / size；再从配置的 warehouse **路径**重新打开根并与构造 inode 比较（不得用操作开始时的旧 FD 代替），用该新根 FD 安全重走 key，确认与本次 final 为同一 inode；失败则用父目录 FD 删除**本次**新建 final。成功返回的 `ObjectRef.uri` 必须立即可 `lookup` / `open_read`（含重启后的 Adapter）。
- `open_read` / `lookup`：对同一个已打开 FD 计算 SHA-256 / size（`open_read` 校验后 `seek(0)` 再返回只读 handle）。
- 无任意 delete / overwrite；不属于当前操作的 orphan 不在写入路径清理。
- 测试只用 pytest `tmp_path`；不访问 `/mnt/*`、网络或数据库。

## PyIcebergCatalogAdapter（C2）

- 运行时入口只有 `open_postgres_catalog_adapter(settings, registry)`：`Settings.catalog_uri` 必须是 PostgreSQL DSN，warehouse 为 settings 的 `file://`；无任何降级。连接 / 数据库故障 → `CatalogUnavailable`（不回显 DSN）。测试可把临时 SQLite `SqlCatalog` 显式注入构造函数，但 SQLite 结果不是 PostgreSQL 证据。
- `TableDefinitionRegistry`：`RegisteredTableDefinition` 把 `(definition_id, version)` 绑定到 PyIceberg Schema、partition spec、表属性与版本化 batch 指纹规则；`definition_hash` 由这些内容的规范 JSON **派生**。生产表与生产指纹规则见下文 C3 / D3B。
- 建表时绑定写入 Iceberg 表属性；每次访问都重新从登记表解析并核对 Schema / partition / format version / 自有属性，不符 fail closed（`UnknownTableDefinition` / `CatalogIntegrityError`）。
- batch id / 指纹 / 行数 / 指纹规则写入 Iceberg snapshot summary；重放与幂等从 main 分支 snapshot 祖先链恢复，无 sidecar。
- 每表固定 `commit.retry.num-retries=0`：PyIceberg 0.12 的自动重试会丢弃 `AssertRefSnapshotId` 并 rebase，破坏 `expected_parent_snapshot_id` 语义。冲突由 PyIceberg 需求检查与 SQL catalog 的 `metadata_location` CAS 判定，映射为 `CommitConflict`。
- 已知限制：namespace 不应命名为 `staging`（默认 staging 目录是 `warehouse/staging`）；orphan 文件只由后续显式 maintenance 清理。

### 本机 PostgreSQL 资源（H12 授权，2026-09-24）

- `hlens_iceberg_catalog`（production-like）与 `hlens_iceberg_catalog_test`（集成测试）：各自同名 LOGIN role 拥有，无 superuser / createdb / createrole / replication / bypassrls，`REVOKE ALL … FROM PUBLIC`；不存在 `hlens_control`。
- 由 `uv run python -m infrastructure.catalog.provision_local_postgres {catalog|test}` 创建：拒绝覆盖已有资源；只向 PostgreSQL 发送客户端计算的 SCRAM verifier；DSN 写入 Git 忽略、权限 600 的 `.env.catalog` / `.env.catalog-test`，从不打印。
- PostgreSQL 集成测试（未设置变量时显式 skip，skip 不算证据）：

  ```bash
  set -a; . ./.env.catalog-test; set +a   # 不要 echo 变量
  uv run pytest -q -m postgres tests/infrastructure/catalog
  ```

  每个测试用唯一 PyIceberg `catalog_name` 与 `tmp_path` warehouse，结束时经 PyIceberg 删除自己的表 / namespace；fixture 拒绝非 `*_test` 数据库。

## Phase 1 生产表（C3）

- 唯一入口：`infrastructure.catalog.PHASE1_TABLES` / `PHASE1_REGISTRY`（`phase1_tables.py`）——03-data.md §7.1 的十五张表（前八张为 C3 首切片且定义哈希不变，中四张为 D3B 追加，第 13 张为 E2 追加，第 14 张为 QG-1 追加，第 15 张为 DS-1 追加），`definition_id` = 表名，`version = 1.0.0`，初始分区按冻结值（`raw.binance_spot_agg_trades` / `canonical.trades` / `research.dataset_selections`：identity `symbol` + day(`event_time`)；`raw.binance_spot_klines_1m` / `canonical.bars_1m`：identity `symbol` + day(`interval_start`)；`quality.availability_evidence_gaps`：identity `subject_symbol` + day(`subject_start`)；其余不分区）。C2 的 test-only 定义只在 `tests/` 中。
- Schema：字段 ID 在源码中逐个写出并等于 Iceberg 建表时分配的 ID（否则 import 失败）；每列带 Iceberg `doc`；时间一律 `timestamptz`（微秒、UTC），交易所十进制值一律 `decimal(38, 18)`，时长为整数微秒，无浮点。列与契约的映射见 `phase1_tables.py` 模块文档；定义哈希由 C2 的规范定义文档派生，golden 值在 `tests/infrastructure/catalog/test_phase1_tables.py`。任何 Schema / 分区 / 属性 / 规则变化 = 新定义版本。
- 幂等建表：`ensure_phase1_tables(adapter)`；已存在且绑定相同则只核对、不改动；任何漂移 fail closed。操作入口（不打印 DSN）：

  ```bash
  set -a; . ./.env.catalog; set +a   # 不要 echo 变量
  uv run python -m infrastructure.catalog.create_phase1_tables
  ```

### Batch 指纹规则 `hlens.pyarrow-batch-sha256@1.0.0`

- 只接受有界 `pyarrow.Table`；按列逻辑拼接（与 chunk 切分无关）后对规范的**逻辑**编码做 SHA-256：null 位图、定宽值（null 槽位为 0）、字符串长度 + UTF-8 字节、list 长度 + 展平值、struct 子列（合并父级 null）。不使用原始 Arrow IPC 字节（null 槽位与切片外字节未定义）、`repr()`、pickle 或调用方指纹；Schema / 字段 metadata 不参与。
- 只接受生产 Schema 需要的类型：`bool`、`int64`、`timestamp[us, UTC]`、`decimal128`、`large_string`、`large_list`、`struct`；其它类型与非空列中的 null → `BatchRejected`。只在 little-endian 主机运行。
- 稳定性边界：以锁定的 `pyarrow==25.0.1` 的 golden vectors 与独立的逐字节推导测试证明；升级 PyArrow 必须保持 golden vectors 不变，编码或类型集合的任何变化 = 新规则版本（新表定义版本）。

### 显式 partition-spec 演进（infrastructure-only，不在核心 Protocol 中）

- `PyIcebergCatalogAdapter.evolve_partition_spec(source, target)`：`target` 必须以 `evolves_from` 绑定已登记的 `source`，且只允许 partition spec 与由此导致的 version / hash 不同（登记表构造时即校验）；目标定义显式声明 Iceberg 将分配的分区字段 ID 与 spec ID。
- 当前表必须按 `source` 完整核对通过；新 spec 与 `hlens.definition.version` / `hash` 在同一个 PyIceberg transaction 中暂存、提交前与 `target` 比对、一次 metadata commit（重试固定为 0）。失败时表保持 `source`；已在 `target` 时幂等返回。不 drop / recreate，不重写数据文件，旧文件保留旧 spec。演进目标不能直接建表。
- 限制：SQL catalog 的 CAS 只保护 spec 需求，不保护其它属性；提交后重新核对，若并发篡改则 `CatalogIntegrityError`（每表单 writer，ADR-0023 §7）。

### 按天分区写入依赖（D-32 → ADR-0026）

PyIceberg 0.12 写入 `day` / `month` / `year` / `hour` / `bucket` 分区需要其官方 extra `pyiceberg-core`。ADR-0026 把直接依赖定为 `pyiceberg[pyarrow,pyiceberg-core,sql-postgres]`（03-data.md §6.1）：`pyiceberg-core` 不是独立顶层依赖，版本由 PyIceberg 0.12 声明的约束（`>=0.10.1,<0.11.0`）解析，`uv.lock` 固定为 `0.10.1`。升级 PyIceberg 时须同时核对该约束。五张 `identity(symbol) + day(...)` 表的 PostgreSQL 用例全部实际写入，并断言真实的 `(symbol, day)` 分区值、replay 同 snapshot 与重启读取。

## BinanceSpotArchiveCollector（D0）

- 入口：`infrastructure.collector.BinanceSpotArchiveCollector`（可用 `from_settings(settings, storage)` 机械工厂）。
- 只访问构造时注入的 archive base（`HLENS_BINANCE_ARCHIVE_BASE_URL`）；descriptor 固定
  `binance.spot.public-archive@1.0.0`，唯一 source `binance.public.spot.archive@1.0.0`，
  `network_origins` = 该 base 的 HTTPS origin。
- 支持范围：`BTCUSDT` / `ETHUSDT` × `agg_trades` / `klines_1m` × UTC 午夜对齐的整日半开区间；
  其它 symbol / type / 非整日边界 / 未声明 source → `UnsupportedRequest`。
- 每个 symbol/day：先 GET `.CHECKSUM`（404/410 → 显式 `SOURCE_ABSENT` gap，不拉 ZIP），
  校验单行 ASCII sha256sum 后，再流式 GET ZIP，经 `StorageAdapter.stage(expected_sha256=…)` →
  `publish` 原子交付；禁止跟随 redirect。
- **内容寻址对象 key（D2 §3 修复 D0 的延期义务）**：`raw/binance/spot/archive/revisions/<sha256>/daily/<官方尾部>`。
  key 在官方 `.CHECKSUM` 校验通过**之后**才构造（sha256 必须是 64 位小写十六进制，否则 `CollectionFailed`），
  basename 仍是官方 `.zip` 文件名（D1 的 key 校验不变）。同 URI + 同 checksum → 同一 key（`publish` 幂等）；
  同 URI + 新 checksum → **另一个**不可变对象，旧对象不覆盖、不删除，因此归档替换不再撞 `ObjectConflict`，
  D2 的 append-only revision 才能成立。路径只由 `(data_type, symbol, day, sha256)` 生成，不接受外部注入。
- 这一变化外部可观察（`CollectionResult` 的 `ref.key` 变了），因此 collector 版本升为 `1.1.0`；
  **`SourceBinding` 不变**（`binance.public.spot.archive@1.0.0`）：Binance 发布的内容与协议没有变化，
  变的只是本机对象布局。D0 的 origin / checksum 先验校验 / 原子发布 / TOCTOU / 流重试性质全部保留。
- 每个 checksum / ZIP GET 的整次尝试（send → 状态 → 完整读或完整流入 stage）受同一
  `http_max_retries` 预算（总尝试 `1 + retries`）；中途 `ReadError`/timeout 可重试，
  redirect / 普通 4xx / 校验与存储冲突不重试。两类 GET 预算独立，ZIP 重试不重取 checksum。
- 构造时严格校验 archive base：拒绝凭据、query、fragment、危险 path；可带安全 path prefix；不静默改写。
- **诚实边界（本批不做）**：不解析 ZIP / 行字段、不构造 revision、不写 Iceberg / Raw、
  不调用 market-data REST、不补尾、不接 WebSocket、不使用 `HLENS_BINANCE_MARKET_DATA_BASE_URL`。
- 测试一律 `httpx.MockTransport` + 真实 `LocalFileStorageAdapter`（`tmp_path`），不访问公网。

## Binance 归档 parser（D1）

- 入口：`infrastructure.parser.parse_archive(request, storage)`（经 `StorageAdapter.open_read` 读取后按字节重算 SHA-256 / 长度，解析的正是被哈希的字节）与 `parse_archive_bytes(request, data)`；`ArchiveParseRequest.for_collected_object(collected, data_type=…, archive_revision_id=…)` 由 D0 `CollectedObject` 机械构造。
- 版本登记：`PARSER_BINDING` = `PolicyBinding(role=parser, binance.spot.archive.parser, 1.0.0, policy_hash)`；`policy_hash` 是 `PARSER_SPEC`（单位规则、覆盖零容差、列布局、字段文法、CSV / ZIP 规则、资源上限）规范 JSON 的 SHA-256，golden 值在 `tests/infrastructure/parser/test_binance_archive_parser.py`。任何规则或上限变化 = 新 parser 版本。
- 支持范围：`BTCUSDT` / `ETHUSDT` × `agg_trades` / `klines_1m` × 一个 UTC 整日；超出范围的请求抛 `UnsupportedArchiveRequest`（调用方错误，不是质量事件）。
- 时间单位只由数据类型 + 覆盖日决定：早于 `2025-01-01T00:00:00Z` 毫秒，自该日起微秒；aggTrades 时间与 kline 开盘时间必须在 `[coverage_start, coverage_end)` 内（零容差）；kline 开盘对齐整分钟，收盘 = 开盘 + 1 分钟 − 1 tick。
- 不可信输入：恰好一个与请求同名的普通 CSV 成员；拒绝前缀 / 尾随字节、注释、zip64、加密、未知标志 / 压缩方法、目录 / symlink、本地头与目录不一致、超限（压缩 1 GiB / 解压 3 GiB / 压缩比 50 / 行 1024 字节）；读到 EOF 后独立核对字节数与 CRC-32（`zipfile` 在声明大小偏小 / 偏大时不报错）。CSV：ASCII、LF 结尾且末行必须有 LF、无 CR / 空行 / header / 引号；整数 / 十进制（≤ `decimal(38,18)`，不经 float）/ `True`|`False` 严格文法；价格为正、aggTrade 数量为正、OHLC 与 taker ≤ total 不变量；aggTrade id 严格递增、时间不减、成交 id 区间不重叠；kline 开盘时间严格递增。缺失分钟与 aggTrade id 间隙不是 parser 拒绝（属后续质量规则）。
- 结果：成功为 `ParsedArchive`（绑定 parser / 归档 revision / symbol / data type / coverage / `ObjectRef` / 成员名 / 单位，`rows` 为与 C3 Raw 表同名同类型的原生列 + `archive_line_number` + 换算后的 UTC 时间列）；任一失败为 `ArchiveRejection`（稳定 `RejectionCode`、行号 / 列名，不含原始行内容），`quality_event()` 给出与 `quality.data_quality_reports.events` 逐字段对应的可持久化事件。对象缺失等存储故障原样抛出。
- **诚实边界（本批不做）**：不构造 revision / `observation_key` / `arrival_seq`、不写 Iceberg、不持久化质量报告、不访问网络；月归档不在 1.0.0 范围内。

## Raw revision store（D2）

`infrastructure/revision/` 把一个 D0 `CollectedObject` + 一个**严格** D1 parse outcome 变成不可变的 append-only revision
（ADR-0023 §4 / §7，03-data.md §7.1 / §7.4）。入口：`RawRevisionStore.ingest(collected, ArchiveContext)`
（或 `ingest_parsed` / `ingest_collection`）。

### 数据流与提交顺序

1. **绑定身份**：从 `(data_type, symbol, coverage day)` 生成官方归档路径，核对 `source_uri` 以它结尾、
   `ref.key` 等于内容寻址 key；并且 `collected.source_sha256` **必须存在**、是规范小写 SHA-256、且等于 `ref.sha256`。
   任一不符在任何 snapshot 产生前 fail closed，不写任何东西。
   归档 payload hash 是**来源声明**（官方 `.CHECKSUM`，D0 已对本机字节验证过）：缺少该声明时绝不用本机对象哈希冒充，
   持久化的 `source_sha256` 列写的也正是这个已验证的来源声明（D2-R1）。
2. **archive revision**：一行稳定 batch 写入 `raw.binance_spot_archives`，**这次提交本身就是序号 block 的分配**。
3. **parsed rows**：按有界 `pyarrow.Table` microbatch（默认 25 000 行，上限 250 000）写入
   `raw.binance_spot_agg_trades` / `raw.binance_spot_klines_1m`，顺序固定在 archive 行之后。
4. **competing heads**：从该 `observation_key` 的全部归档 revision 重建 `RevisionGraph` 并算 maximal heads；
   多于一个即在结果里报 conflict，**不选头**（ADR-0023 §5）。

解析被拒绝（`ArchiveRejection`）时：不写 archive revision，也不写任何 parsed 行。ADR-0023 的失败语义规定失败 payload
**不获得 `knowledge_time`**，而归档行没有 `knowledge_time` 就不能存在。已校验的不可变对象保留；质量事件只返回，
**不持久化**（quality 表的写入属批次 E，这里诚实地不做）。

### 身份（`identity.py`，版本化 + 可哈希）

`IDENTITY_SPEC` 是规范 JSON，`IDENTITY_HASH` 由其完整内容派生（golden 值在 `tests/infrastructure/revision/test_identity.py`）：

| 项 | 规则 |
|---|---|
| archive observation key | `binance:spot:archive:<官方路径>`；**不含** checksum / 下载时间 / 本机路径 |
| aggTrade observation key | `binance:spot:agg_trade:<symbol>:<agg_trade_id>` |
| 1m kline observation key | `binance:spot:kline:<symbol>:1m:<interval_start 微秒>` |
| revision id | `rev1-<sha256>`：identity 规则（id + 版本 + hash）+ observation key + 稳定 source identity + payload hash 的规范 JSON |
| archive payload hash | 官方 `.CHECKSUM` 声明并由存储层本机重算的对象 SHA-256 |
| row payload hash | 原生 CSV 字段 + venue/market/symbol(+interval) + 时间单位的规范 JSON；**不含**行号、到达 / ingest / knowledge 时间 |
| parsed row 绑定 | `source_id = binance.public.spot.archive@1.0.0:<archive revision id>`，另有 `archive_revision_id` 列与固定的 `binance.spot.archive.parser@1.0.0` |

revision id 与到达顺序、墙钟无关：同一输入永远得到同一 id。所有 Arrow 行先构造
`RevisionRecord` / `AvailabilityDecision` / `PrecedenceEvidence` 并经 `RevisionGraph` 校验，再映射到 C3 schema；
没有第二套较弱校验。

### arrival sequence：block 分配，只在 Iceberg 内

- anchor 是 `raw.binance_spot_archives`：用 `max_int64` 流式归约出已提交 `arrival_seq` 的最大值，下一个 **block base** 是
  `(max // stride + 1) * stride`（空表为 0）。stride 冻结为 `2**32`，严格大于 D1 的解压成员上限（3 GiB，每行至少 1 字节 + LF），
  越界或 int64 溢出一律拒绝。
- 归约过程中**逐值** fail closed：必须是非空 int64、非负、不超过 int64 上界，且是 stride 的倍数（合法 block base）；
  任一不符抛 `CatalogIntegrityError`，绝不拿损坏的 anchor 去分配。查重不保存任何无界集合——
  唯一性由 expected-parent 乐观提交与每表单 writer（ADR-0023 §7）保证，这里只证明"已提交的是合法 block base"。
- archive 行用 block base，其 parsed rows 用 `base + archive_line_number`（1-based）。跨三张表全局不重复。
- **archive 行的提交就是分配**：父 snapshot 冲突时重新读取、重新分配、重建 batch，绝不沿用失败尝试的 base（允许间隙，ADR-0023 §4）。
- 崩溃后恢复：从已存在的 archive 行取回同一 base（以及 `ingest_time` / `knowledge_time`），所有 row revision 与 batch 按位重建。
- PostgreSQL 里**没有**任何应用 sequence / counter / journal 表；catalog 库只存 PyIceberg 的 `iceberg_tables` /
  `iceberg_namespace_properties`（有 PostgreSQL 测试断言）。
- `arrival_seq` 只用于审计、幂等与恢复。`RevisionFacts`（precedence 的唯一输入）根本没有这个字段；
  测试用 AST 断言 store 中只有分配 / 恢复 / 写列这几处提到它。

### 恢复步骤（无 journal）

再调用一次 `ingest` 就是恢复过程：

1. 按 revision id 查 `raw.binance_spot_archives`：查到就核对**已持久化的**字段（payload hash、object key/uri/size、
   `source_uri`、symbol、coverage、policy 绑定…），并取回 base 与两条知识轴时间；查不到就重新分配并提交。
2. 用不可变对象 + D1 parser 重新解析（确定性），重建每个 microbatch 与其稳定 batch id。
3. 每个 batch 走 `commit_batch`：adapter 从**实际** batch 独立重算指纹再回答，已提交的返回 `already_committed`，
   同 batch id 异内容 → `BatchConflict` fail closed。因此"只信 batch id"的快路径不存在。

batch id：archive 为 `<revision_id>.archive.<base>`（重试换 base 即换 id），行为 `<revision_id>.rows.<序号>`；
两者都不含随机数、墙钟或当前 snapshot id。

### Policy 与证据

- availability `binance.spot.publication@1.0.0`、precedence `binance.spot.archive-revision@1.0.0`；
  `policy_hash` 均由完整规范 JSON 派生，golden 值在对应测试里。
- 证据文档：`docs/architecture/evidence/binance-spot-publication.md`（只引用 Binance 官方仓库 README 与官方 spot WS 文档，
  记录 URL、访问日期、支持与**不**支持的主张）。
- **1.0.0 的结论**：官方资料没有给出任何具体 revision 的公开时刻（归档只有"次日"的节奏与"可能事后更新"的声明；
  aggTrade 流只有定性的 "Real-time"；kline 的 "2000ms update speed" 是推送节奏不是发布上界），
  因此三类主体全部 `available_time = ingest_time` 并持久化结构化证据缺口（写在冻结的 `availability_evidence_gap` 列里）。
  归档替换同样无法证明先后，一律 **competing heads**。
- **precedence 排序的方向限制（D2-R1，诚实边界）**：`precedence_evidence` 存在其 **newer 一侧**自己的行上，而 revision
  只追加、已提交的行不回写。因此 v1 只持久化"**新到的 candidate supersedes 已知的较旧 revision**"这一个方向。
  若来源语义上较新的 revision 先到、较旧的后到（`SUPERSEDED_BY`），不写任何边：两者都留作 maximal head，
  任何"最新"结论 fail closed。这是保守做法，**不等于**已支持乱序到达的 ordered case——那需要独立的 precedence
  记录（自己的表 + 单独 ADR），绝不能用到达顺序去猜。Binance 1.0.0 生产路径永远没有 source revision id / time，
  始终 unordered，不受该限制影响。
- `source_time` 保持为空：`Last-Modified` / `ETag` 不是官方的 revision 发布时间；原始响应头仍存在
  `source_metadata` 列中供审计（保存 ≠ 采信）。
- `knowledge_time` 取注入 clock 的实际 UTC 时刻，必须 `>= ingest_time`（= D0 的 `retrieved_at`），
  否则 fail closed；clock 不参与任何确定性身份或哈希。

### 限制（诚实边界）

- 本批只写 Raw；**不**做 Canonical、不做通用 PIT 查询、不建质量 taxonomy、不持久化质量报告与 manifest。
- 每表单 writer（ADR-0023 §7）；并发只由父 snapshot 乐观冲突 + 有界重试兜底。
- `PyIcebergCatalogAdapter.scan_columns` / `max_int64` 都是 infrastructure-only 的有界读取，**不在** `core` 的
  `CatalogAdapter` Protocol 里（通用读取接口由批次 F 的首个消费者定义）。
- 序号 anchor 的读取是**流式归约**，不是整表物化：`max_int64` 用 PyIceberg 的 `to_arrow_batch_reader`
  逐个有界 record batch 折叠出最大值，跨 batch 只保留一个 Python int，因此归档历史再长也不会被拼成一个
  `pa.Table`（旧实现走 `scan_columns(...).to_arrow()`，会整列物化；回归测试见下）。
  **仍然存在的成本**：每次分配都要打开全部匹配的数据文件并读取该投影列，所以 I/O 随归档文件数线性增长；
  真正收窄（清单级 min/max 剪枝或独立 anchor）仍是 E/F 的工作。
- 行级契约构造是每行一次 Pydantic 校验：正确但不便宜；大体量 BTC 日归档的吞吐 / 内存基线仍是批量 backfill 前的前置工作。

## REST 纯规则（D3B）

ADR-0027 的第一批实现，全部是**纯函数**：无 HTTP、无 decoder、无 collector / store / reconciler、无 Iceberg 写入、不读时钟。

- `catalog/phase1_tables.py` 追加四张表：`raw.binance_spot_rest_responses`（不分区）、`raw.binance_spot_rest_agg_trades` /
  `raw.binance_spot_rest_klines_1m`（identity `symbol` + day）、`raw.binance_spot_precedence_evidence`（不分区，完整 `PrecedenceEvidence`
  两端显式）。golden 哈希与布局见 `tests/infrastructure/catalog/test_phase1_tables.py`；前八张的哈希回归断言不变。
- `revision/rest_identity.py`：`hlens.binance.spot.rest-revision-identity@1.0.0`，**不 import** `identity.py`
  （其 `IDENTITY_HASH` 不变）。`RestPageQuery` 即参数白名单（隐式"最新"模式不可构造）；规范页身份与 `page_identity_sha256`；
  与归档逐字符相同的元素键与 payload 文档（跨模块一致性测试）；`revision_id`；不含时间的 `edge_id`；REST `arrival_seq`
  区间 `[2**62, 2**63)`、block 分配与区间守卫。
- `revision/rest_availability.py`：`binance.spot.rest-publication@1.0.0`，全部主体 `available_time = ingest_time` + 证据缺口。
- `revision/rest_precedence.py`：`binance.spot.rest-revision@1.0.0`，只有 replay 与 competing heads，从不产生边。
- `revision/channel_precedence.py`：`binance.spot.delivery-channel@1.0.0`（D-33 A）。`compare_channels` 给出
  `EQUAL` / `MISMATCH` / `INCOMPARABLE` / `INTEGRITY_VIOLATION`（后者由 D3E 转为 `CatalogIntegrityError`）；
  `build_channel_edge` 只接受 `EQUAL`，边的 `knowledge_time` 由调用方给出且不得早于两侧 revision。

## exchangeInfo 快照与 listing 历史（E2，ADR-0029）

ADR-0029 方案 A 的实施批次。新增标识符：source `binance.public.spot.exchange-info@1.0.0`、collector
`binance.spot.public-exchange-info@1.0.0`、decoder `binance.spot.exchange-info.decoder@1.0.0`、身份规则
`hlens.binance.spot.exchange-info-identity@1.0.0`、availability policy `binance.spot.exchange-info-publication@1.0.0`、
listing 推导 `binance.spot.listing-status@1.0.0`、precedence policy `binance.spot.listing-observation@1.0.0`。
每个规则的哈希由其规范 JSON 派生，golden 值在对应测试中。

- `catalog/phase1_tables.py` 追加第 13 张表 `raw.binance_spot_exchange_info`（不分区）；前 12 张的定义与哈希不变
  （回归测试 `test_earlier_goldens_are_unchanged_by_e2`）。`canonical.instrument_listings` 定义不变。
- `collector/binance_exchange_info.py`：`GET /api/v3/exchangeInfo`，唯一参数 `symbols=["BTCUSDT","ETHUSDT"]`
  （`ExchangeInfoQuery` 即白名单；URL 构造后按白名单重新解析）；其它 source / symbol 集合 / 参数在联网前拒绝。
  线路复用已验收的 D3D wire（自有 client、无 redirect / cookie / auth / 环境代理、有界重试、`Retry-After`、响应头白名单）。
  响应字节为内容寻址不可变对象，逻辑尝试为一个不可变快照 checkpoint；同一 `request_id` 重放不联网（严格复核并重新解码），
  decoder 拒绝是稳定失败，**不产生** Raw revision。
- `parser/binance_exchange_info.py`：严格 JSON（重复键、NaN、小数 / 指数数字拒绝），只读 `serverTime`（原样保存、不解释单位）
  与每个请求 symbol 的 `symbol` / `status` / `baseAsset` / `quoteAsset`；未请求或重复的 symbol 拒绝整份快照；
  缺失 symbol 与未知状态是事实，不在此解释。
- `revision/exchange_info_store.py`：每份已提交快照一条 Raw source revision（请求身份 + 字节；同字节即重放），
  `available_time = ingest_time` + 证据缺口，`arrival_seq` = 已提交最大值 + 1，单行 batch `<revision>.snapshot.<seq>`，
  expected-parent 提交与有界重试。`ExchangeInfoRowVerifier` 把每行按**其首次交付的 checkpoint** 逐列重建，
  并证明表历史只有单行追加（无删除、无外来 batch）。
- `canonical/listing_rules.py` + `canonical/listings.py`：`TRADING` → listed；`HALT` / `BREAK` / `END_OF_DAY` /
  `CANCEL_ONLY` → suspended（在观察时刻关闭区间）；未知状态、缺失 symbol、base / quote 不符 → 不推断、finding、
  universe fail closed 直到下一次明确观察；永不产生 `delisted`。`tradable_from` = 首次观察到 `TRADING` 的
  `retrieved_at`（observed-from，写入 `status_reason` 与证据缺口），退化 episode 键。只在映射状态变化时追加 revision，
  `supersedes` 上一条（`retrieved_at` 严格递增；同一时刻两份快照无法排序 → 链停止）。推导是快照集合的纯函数；
  一次推导一个 batch `binance.spot.listing-status@1.0.0.from.<Raw snapshot id>`，证明时按该 Raw snapshot 重新推导
  全部已提交 batch。迟到快照若移动了已提交的变化点，旧 revision 保留并报告 `listing_history_diverged`，
  其可用之后的读取为 competing heads（fail closed）。
- `ListingDeriver.listing_at(symbol, simulation_time, knowledge_cutoff)`：universe 构建消费的 PIT 读取；
  首次本机观察之前一律 `no_visible_listing`（历史可用性证据缺口，ADR-0029 开放义务）。
- **诚实边界**：finding 只在结果中返回，写入 `quality.data_quality_reports` 属质量写入器；未做 ADR-0029 要求的
  只读 smoke（L4 字段与 `serverTime` 单位）——测试只用 mock transport。

## quality.availability_evidence_gaps 表（QG-1，ADR-0031，仅表结构）

`catalog/phase1_tables.py` 追加第 14 张表 `quality.availability_evidence_gaps`：每条
`AvailabilityEvidenceGap`（`quality_report_id` + `table` + `revision_id` + `gap`）一行，另带写入它的批次序号
`batch_index`（QG-R1：报告按批次逐批核对行，内存以一批为界），分区
`identity(subject_symbol) + day(subject_start)`，前 13 张的定义与哈希不变（回归测试
`test_earlier_goldens_are_unchanged_by_qg1`，镜像 E2 的 `test_earlier_goldens_are_unchanged_by_e2`）。
本批只交付表定义；把质量规则升到 `hlens.quality.canonical-partition@2.0.0` 并分批写入该表属
`quality/reporter.py` 的实现范围，不在此列。

## research.dataset_selections 表（DS-1，ADR-0033，仅表结构）

`catalog/phase1_tables.py` 追加第 15 张表 `research.dataset_selections`：`DatasetBuilder` 一次构建写入的行，
每行一个 key 的选中结果——`selection_id`（批次号 = 构建 id）、`canonical_table` + `revision_id`（指向选中的
Canonical revision，不复制 payload）、`observation_key`、`event_time`，以及该选中在其中生效的 simulation 区间
`effective_from` / `effective_until`（两者皆空 = point simulation），分区 `identity(symbol) + day(event_time)`
（与 `canonical.trades` 一致）。这是 `research.dataset_manifests` 绑定的 `DatasetRef` 表；表形状原为
`infrastructure/dataset/selection.py` 的提议（`SELECTION_SCHEMA`），DS-1 把它登记为生产表后，`selection.py`
反向从 `catalog/phase1_tables.py` 导入该 schema，两处不再重复定义。前 14 张的定义与哈希不变（回归测试
`test_earlier_goldens_are_unchanged_by_ds1`，镜像 QG-1 的 `test_earlier_goldens_are_unchanged_by_qg1`）。
`tests/infrastructure/dataset/dataset_support.py` 的 F2 / F3 测试改用该生产表，不再另建同形状的 test-only 表。

## 规模性能（G3-P，语义不变）

受限探针（≤ 10k 行、SQLite catalog、2500M 内存上限；前 → 后）：

- **PIT key closure 读**（`pit/selector.py`）：不再用一个 `In(observation_key, 全部 key)` 扫描（PyIceberg 超过 200 个字面量即不按统计裁剪，且 key 前 16 字符相同、截断的字符串统计本就无法裁剪；每个数据文件都要重绑定 / 转换整个集合），改为按 UTC 日只读 `(observation_key, 时间)` 定位含窗口 key 的小时、再按小时（每次 ≤ 10 万行）读整行并以 Arrow `is_in` 本地过滤；结果行集合与旧扫描逐行相同（`tests/infrastructure/pit/test_selector_scale.py`），逐 key 链扩展（`_key_closure`）不变。按 ≤ 200 key 分块扫描实测更慢（1 万 key 时 5.3 s 对 1.3 s），未采用。10k 笔成交、40 个数据文件：1 天窗口（1 万 key）2.64 s → 0.76 s；3 小时内成交的 1 小时窗口（3,333 key）0.92 s → 0.41 s；单文件 1 天窗口 0.29 s → 0.22 s，单文件 1 小时窗口 0.031 s → 0.041 s（多两次窄扫描）。
- **Feature runner**（`feature/runner.py`）：未改代码。700 根 bar × 700 个评估时刻 4.20 s；约 90% 是每次调用对子请求整个前缀各算一次的 `content_hash()`（Provider 的 `FeatureResult.build` 与 `check_answers`）及子请求校验，均属核心契约、随前缀线性增长；在 runner 内增量构建可见集合只快约 1%（4.15 s），未保留。去掉这部分需要改核心（例如缓存冻结契约的 `content_hash`），不能靠削弱检查。
  G3-P2 已在核心做了这件事（`core/domain/base.py` 逐实例记忆 `content_hash()`；`FeatureRequest` 的哈希输入逐条复用各观察实例记忆的规范 JSON，与原算法逐字节相同），runner 未改：同机实测 700 × 700 5.32–5.49 s → 1.11–1.42 s（首次运行含各观察片段的一次计算），200 × 200 0.55 s → 0.12 s；剩余主要是 Provider 自身计算与子请求校验（仍逐次执行，未跳过）。
- **Normalize / 行证明**（`revision/row_integrity.py`）：归档行逐行 `slice().to_pylist()` 改为一次 `take`（首个失败行与报错不变），`batch()` 的列漂移检查改为先比较键集合。10k 行归档单批 normalize 2.23–2.33 s → 2.03 s；冷 1 小时 PIT 选择（含单元证明）2.05–2.10 s → 1.81 s。多批次时成本主要在 PyIceberg 每次扫描重读全部 manifest（40 批 normalize 约 15 s、约 8 千次 manifest 打开），不在本批范围。
