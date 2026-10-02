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
