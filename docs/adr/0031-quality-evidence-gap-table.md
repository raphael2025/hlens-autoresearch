# ADR-0031: 质量报告的证据缺口写入独立只追加表（D-QGAP）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（"一切都你自己决定"；红线除外） |
| 起草者 | Claude Code（Opus），Phase 1 G3 容量工作中发现 |
| 相关 Phase | Phase 1（roadmap 验收 #16、#18；G3 容量基线） |
| 影响范围 | Data / Infrastructure（新增一张 additive 表；质量规则升 2.0.0） |
| 是否破坏兼容 | 否：已冻结的 13 张（含 E2 新表）定义与哈希不变；`quality.data_quality_reports` 结构不变 |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md) §2、[ADR-0028](0028-dual-raw-canonical-lineage.md) §7 |

## 背景

E3 的质量规则 `hlens.quality.canonical-partition@1.0.0` 把分区内**每条**带证据缺口的 Canonical revision 写进报告行的
`evidence_gaps` 列表。官方资料不能证明历史行情的公开时刻（D-HIST），所以归档行全部带缺口：1 分钟 K 线每天 1 440 条，
成交每天 100～300 万条。G3 探针显示：成交一天的一行报告会达到数 GB，本机（WSL 约 15 GB，与其它进程共用）放不下，
内存不足已多次导致会话中断。

## 裁决

1. 新增 additive 表 `quality.availability_evidence_gaps`（按 `identity(subject_symbol), day(subject_start)` 分区）：
   每条 `AvailabilityEvidenceGap` 一行——`quality_report_id`、`table`、`revision_id`、`gap`、`subject_symbol`、
   `subject_start`，以及写入它的批次序号 `batch_index`（long，必填，从 0 起；QG-R1 增补）。稳定身份仍为
   `(table, revision_id)`，语义与 `core/contracts/universe.py` 的 `AvailabilityEvidenceGap` 完全一致（不改契约）；
   `batch_index` 只是写入方的记账列，`evidence_gaps_of` 不返回它。
2. 质量规则升为 `hlens.quality.canonical-partition@2.0.0`：
   - 报告行的 `evidence_gaps` 列恒为空列表；
   - 缺口按时段分批写入新表，批次号 `<report_id>.gaps.<序号:08d>`（每批 ≤ 25 000 行，批内按 `revision_id` 排序，
     批次按时段顺序编号），**先于**报告行提交；
   - 报告行增加一条 `evidence_gaps` 事件，写明缺口条数与批次数（确定性 event id）；
   - **报告行是唯一引用**：没有对应报告行的缺口批次（例如证明中途失败留下的）不属于任何报告，读取方忽略；
   - **逐批按行核对**（QG-R1；写入与复用两种模式都做）：每批写入（或复用时核对其唯一提交快照的指纹与行数）后，
     立即扫描 `quality_report_id = 报告 ∧ batch_index = i` 的行（至多一批，≤ 25 000 行），它们必须与该批应写的行
     **完全相同**（作为多重集合比较：按规范行排序后逐行相等）；全部批次之后，再只扫描该报告的 `batch_index` 一列
     （int64）：行数必须等于已写行数，且不得出现 ≥ 批次数的序号、不得缺少任何序号。于是任何被删除、改写、换批或
     中途新增（包括读取批次历史之后才出现）的行都会被发现；内存以一批为界，不再对全部缺口行求和摘要；
   - 已提交的报告被复用时，只核对缺口批次（按上条逐批核对，并与报告事件的条数一致），从不补写；缺失或不符即完整性错误；
   - 缺口批次提交遇到并发冲突（`CommitConflict`）时，与报告行一样进入报告的重试循环；已写入的批次按批次号幂等重放并重新核对。
3. 1.0.0 的规则、代码语义与已写出的报告行不变（历史只追加）；新报告一律用 2.0.0。

## 备选方案

| 方案 | 内容 | 结论 |
|---|---|---|
| **A（采纳）** | 独立只追加表，逐条保存，报告行只存引用与计数 | 语义不变、内存按时段有界 |
| B | 同一原始单元的缺口合并为一条（条数 + 范围） | 拒绝：改变"逐条绑定"的含义 |
| C | 成交报告按小时出 | 拒绝：改变报告粒度，数据集要绑定大量报告 |

## 后果

- 正面：报告内存按时段有界；缺口仍逐条可查、可被 manifest 绑定。
- 负面：多一张表；读取缺口要按 `quality_report_id` 查询；未完成的报告会留下未被引用的缺口批次（无害）；
  他人写入的带本报告 `quality_report_id` 的行会使该报告永久无法写出或复用（fail closed，而非静默接受）。
- 修订记录：QG-R1（2026-09-25，只读复核返修）——新增 `batch_index` 列与逐批按行核对，取代原先
  "条数 + Σsha256(行) mod 2^256" 的整报告摘要（后者不是可靠的多重集合哈希，且需要一次读出全部缺口行）。
  该表此前没有任何数据，定义哈希随之更新；前 13 张表的定义与哈希不变。
- 开放：`ResearchDatasetManifest` 仍逐条列出证据缺口（冻结契约）——成交级数据集的 manifest 规模问题留给 F3 / Phase 2
  （届时如需改契约，另立 ADR）。

## 合规检查

- [x] 不修改 Domain Contract（`AvailabilityEvidenceGap` 语义与字段不变）
- [x] 不修改 Validation Constitution / Profile
- [x] 不删除历史（1.0.0 报告保留）
- [x] PostgreSQL 不存行情（新表在 Iceberg warehouse）
