# ADR-0101: Dataset v3 生产入口接线

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权） |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 1（Market Representation） |
| 影响范围 | `infrastructure/dataset/`、`infrastructure/quality/`、`infrastructure/tools/`（均为新增入口）、`apps/worker/`（薄 job）、Phase 1 测试迁移；不改 `core/` |
| 是否破坏兼容 | 否（新增入口；v2 写路径保持 ADR-0077 DQ-10 的禁用） |

## 背景（Context）

Dataset v3 的构建与校验（`DatasetBuildPipeline`、`StreamingEvidenceVerifier`、`QualityReporterV3`）已实现，但仓库内唯一的构建调用方是 `infrastructure/tools/capacity_probe.py` 的合成探针。生产侧缺：

1. 构建 / 校验 / 查看 v3 Dataset 的命令行入口与 worker job；
2. DQ-9 规则值、`PitRunParams`、`UniverseRunParams`、Quality 限值的运行配置载体（目前散落在探针常量里）；
3. 公开的 Quality identity registry、钉定 PIT spec 的公开函数；
4. exchangeInfo、REST 补尾、v3 质量报告三个上游阶段的入口；
5. P11 `--authority-evidence-verifier` 可指向的 verifier 工厂；
6. 仍对新 selection 调用已禁用的 v2 `DatasetBuilder.build` 的测试。

DQ-9 的**数值**仍 OPEN（ADR-0077），本 ADR 不选择任何数值。

## 决策（Decision）

1. **配置载体（D1）**：新增冻结 dataclass `DatasetBuildProfile`（`infrastructure/dataset/profile.py`），由 `load_dataset_profile(path) -> DatasetBuildProfile` 从显式 JSON 读取。没有任何默认值；缺字段、多字段、类型不符一律拒绝。`profile_hash()` 为规范 JSON 内容哈希。profile 携带 `schema_version` 与可选 `capacity_evidence`（缺省时入口输出 `capacity_evidence=none`，表明它不是已验收配置）。
2. **入口形态（D2）**：主入口为 `python -m infrastructure.dataset.cli {build,verify,show}`；`apps/worker/dataset_job.py` 提供薄 job（幂等键 = `selection_id`），由部署方 factory 注入，仓库不自动注册。入口只走 `DatasetBuildPipeline` / `QualityReporterV3` 等有界路径；**不得**调用 v2 `DatasetBuilder.select/build`、非 `iter_bounded` 的 PIT 选择或 `UniverseBuilder.build`（由架构测试固定）。
3. **Verifier 工厂（D3）**：`infrastructure/dataset/factory.py` 提供 `open_dataset_pipeline(settings, profile)`（上下文管理器）与 `bind_profile(profile) -> Callable[[RevisionCatalog, StorageAdapter], StreamingEvidenceVerifier]`。P11 部署方以 `functools.partial` 写一个三行模块指向它；`research/operations` 不改参数。
4. **Quality scratch（D4）**：位置为 `<settings.canonical_scratch_path>/dataset-quality`，以独立 `LocalFileStorageAdapter` 包装，与证据 warehouse 隔离；不改 `Settings`。
5. **公开构件**：`CanonicalV3IdentityRegistry`（`infrastructure/quality/identity_registry.py`，包装 `report_v3` 的注册哈希）与 `pin_dataset_pit_spec(adapter, *, name, version, simulation_time, knowledge_cutoff, listing_assumption)`（`infrastructure/dataset/pinning.py`）。探针改为使用公开构件属于可选清理，不是本 ADR 的要求。
6. **上游入口**：`infrastructure/tools/listing_cli.py`（exchangeInfo collect → ingest → derive）、`infrastructure/tools/rest_tail_cli.py`（collect → ingest → normalize → reconcile）、`infrastructure/quality/report_cli.py`（v3 质量报告，限值全部来自 profile）。网络采集只在显式子命令下发生；默认打印计划。
7. **v2 测试迁移（D5）**：roadmap #20 e2e 及其 PostgreSQL 变体改走 v3 pipeline；其余 v2 兼容 / 回放用例改用测试侧 `seed_v2_manifest` 夹具。原断言不得削弱（H4）。不为测试重新打开 v2 写路径。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 部署方 `MODULE:CALLABLE` 返回 profile | 灵活 | 配置不可审计、无内容哈希 | 显式 JSON 可哈希、可复核；dataclass 仍可被程序化构造 |
| 新增 `Settings.dataset_scratch_uri` | 配置集中 | 改动冻结的 settings 边界与路径重叠校验 | 子目录即可满足隔离要求 |
| 给 v2 builder 加测试专用写开关 | 测试改动小 | 重新打开 DQ-10 禁用的写路径 | 违背 ADR-0077 |

## 后果（Consequences）

- 正面：v3 Dataset 第一次有可重复的生产入口；P11 真实运行可以接上 v3 证据校验。
- 负面 / 代价：新增约 2000 行代码与测试，未测；DQ-9 数值未定前，入口只能使用显式提供的 profile。
- 需要迁移的内容：约 11 个 Phase 1 测试文件的夹具。
- 对复现性的影响：无；manifest 格式与哈希不变，profile 哈希只出现在入口输出中。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile，不选择 DQ-9 数值（H3）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变（apps 不 import research）

## 实现记录

（追加记录，不改上文决策。分支 `feature/adr-0101-dataset`，2026-10-02。）

- **D1 / D2 CLI / D3 / §5**（`f642806`）：`infrastructure/dataset/{profile,cli,factory,pinning}.py`、`infrastructure/quality/identity_registry.py`。
- **D2 worker job**（`88ce67f`）：`apps/worker/dataset_job.py` 只依赖 `apps` / `core` / 标准库；`dataset_build_job(port, request)` 以 `selection_id` 为幂等键提交 `{"selection_id", "request"}`，处理器重新推导 `selection_id`、不符或结果属于其他 selection 即拒绝，端口失败只记录异常类型。端口为 `infrastructure/dataset/job_port.py` 的 `PipelineDatasetJobPort`（严格 JSON 请求文档，携带已钉定的 PIT spec，须为规范 JSON 形式）。仓库不注册；是否声明 `idempotent=` 由部署方决定。`summary_document` 与 CLI 共用（CLI 输出不变）。架构测试（`tests/test_architecture_boundaries.py`）固定入口模块（dataset cli / factory / job_port、worker job、quality report_cli）不导入 `DatasetBuilder` / `ManifestStore` / `PitSelector` / `UniverseBuilder`、不调用 `.select()`、`.build()` 只在 pipeline（或 job 注入的 port）上。
- **§6 上游入口**（`f0640b3`）：`infrastructure/tools/listing_cli.py`（plan / collect / ingest / derive）、`infrastructure/tools/rest_tail_cli.py`（plan / collect / ingest / normalize / reconcile）、`infrastructure/quality/report_cli.py`（plan / report / verify，限值全部来自 profile，scratch 为 D4 位置）；无子命令时打印计划，只有 `collect` 联网；共用 `infrastructure/tools/cli_support.py`（设置不回显、DSN 脱敏、未知错误只打印异常类型）。各阶段均为既有组件，未重新实现。
- **§7 / D5 测试迁移**（`73ba79d`）：roadmap #20 e2e 新增 v3 版本 `tests/infrastructure/e2e/test_phase1_first_slice_v3.py`（含 PostgreSQL 变体），经 `open_dataset_pipeline` 构建、从 evidence streams 读回 manifest 声明、重开 catalog 后逐位重放，并把 E4/F4 特征绑定到 v3 manifest。v2 e2e 原样保留为 v2 回放 / 兼容用例。其余 v2 兼容 / 回放用例此前已改用测试侧夹具 `seed_historical_v2`（即本 ADR 所称 `seed_v2_manifest` 的角色，未改名）；全仓检索无测试再对新 selection 调用 `DatasetBuilder.build`（除 DQ-10 / 拒绝断言）。未削弱任何断言，v2 写路径保持禁用。
- **实现中发现的事实（未在本 ADR 范围内处理）**：
  1. v3 构建（`DatasetEvidenceBuilder` 的请求检查）仍要求 PIT spec 绑定旧表 `quality.data_quality_reports`；生产 catalog 若只有 v3 报告、旧表无快照，构建被拒。v3 e2e 与既有 v3 测试支撑一样先写旧版报告。
  2. v3 构建的 Quality join 还需要 listing-history 报告（`ListingHistoryQualityReporterV2`）；其限值（prefix / metadata / hash chunk 等）不在 `DatasetBuildProfile` 中，§6 的 `report_cli` 只覆盖 canonical-partition v3 报告，因此生产侧尚无 listing 质量报告入口。
- 检查（仅定向运行）：见各 commit 正文；ruff check / format --check 与 mypy（strict）均通过。

## 修订 1（2026-10-02，PM）：v3 构建的质量来源绑定与 listing 质量入口

实现记录中的两项事实是生产阻断，按以下决定处理（不改契约、不选 DQ-9 数值、不放宽任何质量要求）：

1. **质量来源按其实际使用的表绑定。** v3 构建（`DatasetEvidenceBuilder` / `DatasetBuildPipeline`）的质量 join 读取哪张表，PIT spec 就必须绑定哪张表：分区报告来自 ADR-0093 的 v3 manifest 表时，必须绑定该表（及其 evidence streams 所在位置的规则所要求的表）；旧表 `quality.data_quality_reports` 改为与 Raw evidence / 缺口表相同的规则——**构建运行时它有 snapshot 才必须绑定**，没有 snapshot 时不得要求（也不得为满足要求而写旧版报告）。每个覆盖分区仍必须恰有一份在绑定 snapshot 上可重新推导的报告；同一分区同时有旧版与 v3 报告时，以 ADR-0093 已定的优先 / 兼容规则判定，规则不唯一即失败关闭。既有 v2 / 旧版回放结果按位不变。
2. **listing 质量报告入口。** `DatasetBuildProfile` 增加 listing-history 质量报告所需的限值段（与现有分区质量限值同样：显式、无默认值、缺失即拒绝；`schema_version` 按 additive 升 minor，旧 profile 文件读取时若缺该段，则只有不需要 listing 报告的命令可用，构建命令拒绝并说明原因）。`infrastructure.quality.report_cli` 增加 listing 报告子命令（plan / report / verify 语义与分区报告一致），限值全部来自 profile。
3. 测试：v3-only catalog（旧表无 snapshot）上端到端构建成功；旧表有 snapshot 而 spec 未绑定时拒绝；同分区新旧报告冲突时失败关闭；旧 profile 缺 listing 段时构建拒绝；既有测试断言不变（H4）。
