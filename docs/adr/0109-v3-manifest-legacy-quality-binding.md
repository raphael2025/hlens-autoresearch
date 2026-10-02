# ADR-0109: v3 Dataset manifest 的旧质量表绑定（契约 2.6.0）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，PM） |
| 日期 | 2026-10-02 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 1（Market Representation） |
| 影响范围 | Contract（`core/contracts/universe.py`，契约 2.5.0 → 2.6.0）、`infrastructure/dataset/`、Schema 导出、API / Web DTO 版本登记 |
| 是否破坏兼容 | 否（minor；2.0.0 ~ 2.5.0 载荷保留自己的信封与规则，哈希逐位不变） |

## 背景（Context）

ADR-0101 修订 1 第 1 条要求：v3 构建按质量 join 实际读取的表绑定；旧表 `quality.data_quality_reports` 只在构建时有 snapshot 才必须绑定。ADR-0101 修订 2 发现它与冻结契约冲突而暂缓（D-V3-LEGACY-BIND）：

- `ResearchDatasetEvidenceManifest`（ADR-0077，自 2.3.0）要求上游绑定同时含 `canonical.instrument_listings` 与 `quality.data_quality_reports`；
- `PointInTimeSpec.snapshot_bindings` 的值是非空 snapshot id；`CommitRequest.row_count ≥ 1` 禁止空提交（ADR-0023 §7）。

自 2.5.0（ADR-0094）起，v3 构建的分区与 listing 质量证据全部来自 `quality.data_quality_report_manifests`，旧表不再被读取。但只写 v3 报告的新库里旧表没有 snapshot，无法绑定，于是 v3 构建在生产上无法启动；为满足绑定去写旧版报告或伪造空 snapshot 都被禁止。

## 决策（Decision）

1. **契约 2.6.0（minor）。** `CONTRACT_SCHEMA_VERSION = "2.6.0"`，加入 `PUBLISHED_CONTRACT_SCHEMA_VERSIONS`。唯一的模型规则变化：记录版本 ≥ 2.6.0 的 `ResearchDatasetEvidenceManifest`，上游绑定必须含 `canonical.instrument_listings` 与 `quality.data_quality_report_manifests`，**不再要求** `quality.data_quality_reports`（出现时仍是合法绑定）。记录版本 < 2.6.0 的 manifest 规则不变。其余模型的字段、校验与 Schema 不变；与以往 minor 一样，当前版本新建对象的信封（与哈希）为 2.6.0，旧载荷按记录版本重放、哈希逐位不变。
2. **构建规则（infrastructure）。** 2.6.0+ 的 v3 构建把旧表与 Raw evidence / 缺口表同等对待（`_check_unbound`）：构建运行时它有 snapshot 就必须绑定，没有就不要求；重放按首次构建判定（G2 RT-5 同理）。每个覆盖分区仍须恰有一份在绑定 snapshot 上可重新推导的报告，规则与 ADR-0093 / 0094 相同；不读取、不写入旧表。
3. **不变的部分。** v2 manifest（`ResearchDatasetManifest`）与 < 2.6.0 的 v3 manifest 的重放与验证不变；不为满足绑定写旧版报告；不引入空提交。
4. **验收。**
   - (a) 只有 v3 报告、旧表无 snapshot 的 catalog 上，`pin_dataset_pit_spec` + v3 构建端到端成功，manifest 记录 2.6.0；
   - (b) 旧表有 snapshot 而 spec 未绑定时构建拒绝；
   - (c) 2.5.0 manifest 缺旧表绑定仍被模型拒绝；既有 2.3.0 ~ 2.5.0 v3 golden 与 v2 重放按位不变；
   - (d) Schema 重新导出，DTO 版本登记加入 2.6.0，旧版本仍支持；
   - (e) 全仓门禁全绿；不删断言、不放宽容差、不重钉历史哈希（H4）。新版本新建对象的期望值按以往 minor 的先例逐项登记。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 不改契约，生产前先写一份旧版报告 | 零契约改动 | 为满足绑定写报告，ADR-0101 修订 1 已否决 | 伪造依赖 |
| 旧表建表时写空 snapshot | 绑定可满足 | 违背 `row_count ≥ 1`（ADR-0023 §7） | 违反冻结规则 |
| 2.6.0 只在旧表缺失时写入（数据决定版本） | 现有哈希不动 | 同一代码按数据产出不同契约版本，破坏版本语义 | 不可审计 |
| 继续暂缓 | 无工作量 | v3 生产入口在新库上不可用 | 阻断 Phase 1 生产路径 |

## 后果（Consequences）

- 正面：v3 生产入口不再依赖旧质量表；质量来源绑定与实际读取一致。
- 负面 / 代价：一次全局 minor 升版（Schema 导出、DTO 登记、当前版本期望值），机械但面广。
- 对复现性的影响：旧载荷按记录版本重放，不受影响。

## 合规检查

- [x] 不破坏已冻结契约：minor，旧版本规则与哈希不变
- [x] 不修改 Validation Constitution / Profile / DQ-9 数值（H3）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 实现记录（2026-10-02，`feature/adr-0109-contract-260`，实现 Agent）

按决策 §1 ~ §4 落地；v2 `ResearchDatasetManifest` / v2 `DatasetBuilder`、Validation Profile / Constitution 未改动。

- **契约（§1）**：`core/domain/base.py` `CONTRACT_SCHEMA_VERSION = "2.6.0"`，追加进 `PUBLISHED_CONTRACT_SCHEMA_VERSIONS`；`core/contracts/universe.py` 新增 `ADR_0109_VERSION = "2.6.0"`。`ResearchDatasetEvidenceManifest._manifest_invariants`：记录版本 ≥ 2.6.0 要求 `canonical.instrument_listings` 与 `quality.data_quality_report_manifests`，不再要求 `quality.data_quality_reports`（出现时仍合法）；< 2.6.0 分支逐字保留原规则。无新字段 / 取值 / 模型，`CONTRACT_MODELS` 仍为 148。Schema 经 `python -m core.contracts.registry` 重新导出（信封默认值与该 manifest 的 docstring）；`docs/architecture/02-domain.md` 新增 §2.13 并更新 §3.3，`03-data.md`、`10-migration.md`、`core/README.md`、`core/contracts/README.md` 同步。
- **构建（§2）**：`infrastructure/dataset/builder.py` 的 v3 路径：`_check_evidence_request` 只在给定记录版本 < 2.6.0 时要求旧表（无版本的预检查由随后的带版本检查决定）；`_check_unbound_evidence` 新增必填 `schema_version`，≥ 2.6.0 时旧表与 Raw evidence / 缺口表同列（`_BOUND_IF_PRESENT_V3_260`，拒绝信息标 ADR-0109），持久化 manifest 的重放照旧豁免（按首次构建判定）。`build` 先确定版本（新组 = 当前版本，重放 = 记录版本）再做该检查。`pin_dataset_pit_spec` 本已只绑定有 snapshot 的表，未改。
- **DTO / Web（§4(d)）**：`validation_report` 的 API 与 Web DTO 基线 2.6.0，2.0.0 ~ 2.5.0 仍受支持；`apps/api/openapi.json` 重新导出，`apps/web/src/api.d.ts` 与 `openapi-typescript` 输出逐字一致。六份 2.5.0 控制台 fixture 登记为 `LEGACY_2_5_0`（`regenerate_legacy("2.5.0")` 在 `contract_schema_version_scope("2.5.0")` 下逐字节复现，生成器只新写六份 2.6.0 文件），旧 fixture 文件未改动。
- **当前版本期望值（§4(e)）**：按 2.5.0 先例逐项改为 2.6.0 的只是"当前版本 / 已发布元组 / 当前信封 / Schema 默认值"断言（`tests/test_*` 中 11 个文件，`tests/infrastructure` 中 4 个文件，DTO / fixture 计数与 Web 测试）。依赖当前信封的两处钉值按同文件先例重钉并在注释保留旧值：golden experiment `75c58fd4…` → `e874ba87…`（唯一变化的输出是 `backtest.result_hash`），`test_loop_e2e` 的三个 record hash 与 fingerprint；未修改的旧测试在 `contract_schema_version_scope("2.5.0")`（含 import）下仍通过，helper 在该 scope 下逐字节复现 `75c58fd4…`。没有任何旧版本 golden / 历史哈希被重钉，没有删除断言或放宽容差。
- **验收测试**：(a)(b) `tests/infrastructure/e2e/test_phase1_first_slice_v3.py` 新增 v3-only catalog（从不写旧版报告）端到端构建与重启重放、以及旧表有 snapshot 而 spec 未绑定时拒绝且不提交任何东西；主流程断言 manifest 记录 2.6.0、旧表绑定当且仅当其有 snapshot。(c) `tests/test_adr_0109_contract_260.py`：2.6.0 规则、2.3.0 ~ 2.5.0 缺旧表仍拒绝、2.5.0 载荷不被 2.6.0 规则挽救；新增 2.5.0 七流 v3 manifest golden `tests/golden/v2_5_0/dataset_v3_manifest.json`（由升版前代码 `b0707d2` 生成，哈希 `63c5bd73…`，新代码逐字节复现）；既有 2.3.0 / 2.4.0 golden 与 v2 重放测试未改动且通过。
- **rebase 到 `phase/1`（`372cf31`，再到 `19f04a0`）后**：ADR-0105 新增的 `tests/research/loop/test_dataset_operator.py::test_the_synthetic_operator_identity_is_unchanged` 同样钉住当前信封下的身份，按同一先例重钉（`81f4173b…` → `7f9c91f1…`，旧值在 `contract_schema_version_scope("2.5.0")` 下仍通过）。
- **检查（实际运行，3 GiB 内存上限，TMPDIR 在磁盘）**：rebase 前 `tests/infrastructure/dataset` 648 passed / 5 skipped；`tests/apps tests/research/reports tests/infrastructure/catalog` + canonical 重放 918 passed / 63 skipped；e2e v3 主流程与 (a)(b) 各自通过；dnet 重放、loop e2e、migration 与引用 2.3.0 ~ 2.5.0 的 research 套件 321 个（重钉后通过）。rebase 后 `tests/test_*.py tests/apps tests/research/reports tests/research/loop` 全部通过（上述 1 个重钉后 31 passed）；`tests/research/operations`（除 `test_authority_resolver.py`）+ migration + catalog + canonical 重放 + `tests/infrastructure/pit` + dnet 重放 + loop e2e 800 passed / 61 skipped；dataset CLI / worker / ADR-0077 / ADR-0109 176 passed；Web `npm test` 135 + 127 passed、`npm run build` 通过；`ruff check .`、`ruff format --check .` 通过；`mypy` 883 个源文件无问题；再次 rebase 到 `19f04a0` 后 `tests/test_*.py` + loop P7 / operator / e2e 3377 passed，`mypy` 884 个源文件无问题。未运行：全量测试、`tests/research/operations/test_authority_resolver.py`、`test_versioned_replay_first_slice.py` 与其余 e2e（磁盘争用下单个用例 20 ~ 50 分钟）。
