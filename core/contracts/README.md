# core/contracts

跨 Plane DTO、JSON Schema 导出与 Provider 接口（05-plugin.md、02-domain.md §3）；Provider 接口按 ADR-0017 的节奏交付。所有 Schema 带 schema_version。

`registry.py` 的 `CONTRACT_MODELS` 是"所有核心实体都有契约与 Schema 导出"的唯一来源：
当前 134 个模型导出到 `schemas/` 顶层；`schemas/v1/` 是 v1 只读快照，导出不会写入其中。

`revision.py`（Phase 1 B1，ADR-0023）：双时间、availability / precedence 绑定、append-only revision DAG、
PIT 输入与 maximal-head 结果形状的 8 个契约（见 02-domain.md §2.2）。只有契约与不变量，不含 PIT 选择算法或存储。

`universe.py`（Phase 1 B2，ADR-0024 / ADR-0023 §6）：listing episode / revision、`UniverseSelectionSpec` 及其专用绑定、
成员 / 排除清单与 `ResearchDatasetManifest` 的 13 个契约（见 02-domain.md §2.3）。不含 universe 选择器或 PIT 执行器。

Data Plane Adapter（Phase 1 B3，ADR-0017 / ADR-0021 / ADR-0022）：三个可执行 Protocol 与 15 个 DTO，
每个模块把 DTO 与其 Protocol 放在一起（见 02-domain.md §2.4）。只有接口与 DTO，**没有任何 Adapter 实现**。

| 模块 | Protocol → 方法 | DTO | 首个实现 |
|---|---|---|---|
| `storage.py` | `StorageAdapter`：`stage` / `publish` / `lookup` / `open_read` | `StageRequest`、`StagedObject`、`ObjectRef`、`PublishResult` | C1 `file://` |
| `catalog.py` | `CatalogAdapter[BatchT]`：`load_table` / `create_table` / `get_snapshot` / `commit_batch` | `TableDefinition`、`TableInfo`、`SnapshotInfo`、`CommitRequest`、`CommitResult` | C2 PyIceberg SQL Catalog |
| `collector.py` | `CollectorAdapter`：`descriptor` / `collect` | `SourceBinding`、`CollectorDescriptor`、`CollectionRequest`、`CollectedObject`、`CoverageGap`、`CollectionResult` | D0 归档下载 |

provider-agnostic contract suite 在 `tests/contract_suites/`：实现方继承 `StorageAdapterContract` /
`CatalogAdapterContract` / `CollectorAdapterContract` 并提供 subject fixture 即可复用同一组检查。

`feature.py`（Phase 1 F4，ADR-0030）：`FeatureProvider` Protocol（`descriptor` / `compute`）与 5 个 DTO
（`FeatureObservation`、`FeatureRequest`、`FeatureValue`、`FeatureResult`、`ProviderDescriptor`，见 02-domain.md §2.5）。
执行器在 `infrastructure/feature/`（每个评估时刻只把可见集合交给 Provider），首批实现在 `plugins/features/`；
provider-agnostic suite 为 `tests/contract_suites/feature.py`（`FeatureProviderContract` / `FeatureSubject`）。

`state.py`（Phase 2，ADR-0035；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`StateProvider` Protocol（`descriptor` / `compute`）与 5 个 DTO
（`StateInput`、`StateRequest`、`StateValue`、`StateResult`、`StateProviderDescriptor`，见 02-domain.md §2.6），以及 `StateSpec.method`
参数编码 `state_method` / `parse_state_method`。输入只能是 Feature 值；执行器在 `infrastructure/state/`，首批实现在 `plugins/states/`；
provider-agnostic suite 为 `tests/contract_suites/state.py`（`StateProviderContract` / `StateSubject`）。
`event.py`（Phase 3，ADR-0036）：`EventProvider` Protocol（`descriptor` / `detect`）与 5 个 DTO
（`EventInputPoint`、`Event`、`EventRequest`、`EventResult`、`EventProviderDescriptor`，见 02-domain.md §2.8）。
执行器在 `infrastructure/event/`（每个检查点只交出可见集合，相邻检查点的事件表必须一致：不得未来确认），首批实现在
`plugins/events/`；provider-agnostic suite 为 `tests/contract_suites/event.py`（`EventProviderContract` / `EventSubject`）。
`outcome.py` / `cost_model.py`（Phase 4，ADR-0037）：`OutcomeProvider` Protocol（`descriptor` / `compute`）与 7 个 DTO
（`OutcomeLabelSpec`、`OutcomePriceBar`、`OutcomeEvent`、`OutcomeRequest`、`OutcomeLabel`、`OutcomeProviderDescriptor`、`OutcomeResult`），
以及成本模型 v1 `CostModelSpec`（`kind=cost_model`）。Outcome 载荷带 `label_only` 判别字段，任何输入 DTO 都拒绝它；
实现在 `plugins/outcomes/`，provider-agnostic suite 为 `tests/contract_suites/outcome.py`（`OutcomeProviderContract` / `OutcomeSubject`）。

`loop_audit.py`（Phase 11，ADR-0050）：持续研究循环审计记录的 8 个契约（`LoopBudgetUsage`、`LoopBudgetLimits`、
`LoopStageRecord`、`LoopTransitionRecord`、`LoopOverrun`、`LoopRoundRecord`、`LoopRoundStarted`、`LoopRoundRecorded`，
见 02-domain.md §2.10），以及阶段顺序常量与 `check_stage_order`（worker 原样再导出）。它们**描述 worker 既有的字节**：
`audit_payload()` 不含 `schema_version` 信封，`from_audit_payload()` 要求规范 JSON 逐字节往返，`record_hash` 规则不变；
不去除首尾空白。worker（写入与重放）、`research/reports` 写入器与 `apps/api` 的 `ReportStore` 都按它校验，不合格即拒绝。

Provider 接口的交付节奏由 ADR-0017 定下：Phase 0 只冻结职责、概念输入输出、确定性与版本语义；
每类 Provider 的可执行 Protocol、DTO 与 provider-agnostic contract tests，在首次消费它的 Phase
开始实现之前交付，并计入该 Phase 的验收。

> 当前状态（框架批次，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：研究 Provider 已交付 Feature（F4）、State（P2）、Event（P3）、Outcome（P4）、
> Strategy / Risk / Backtest（P5）、LLM（P7）、Knowledge（P0.5）、SyntheticMarket（P9）的 Protocol；基础设施 Adapter 另交付
> `EventBusAdapter`（P11 地基）；`ComputeEngineAdapter` 待首次消费时交付（ADR-0017）。进度见 PROJECT_STATUS.md。
