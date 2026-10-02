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
