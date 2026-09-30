# ADR-0094: Bounded PIT conflict-head evidence stream

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-30 |
| 决策者 | Codex 项目工程负责人（依 Raphael 授权） |
| 起草者 | Codex |
| 相关 Phase | Phase 1 — Market Representation |
| 影响范围 | Contract / Data / Infrastructure |
| 是否破坏兼容 | 否；仅对契约 2.5.0 新增能力 |

## 背景（Context）

ADR-0077 要求 Dataset v3 / PIT v3 的完整工作集有界，并以可重放证据保留选择依据。当前 `PointInTimeSelection.maximal_heads` 是完整 tuple；一个 observation key 的 maximal heads 数量没有固定上限，因此即使图算法改为外存流式，结果 tuple 仍可随输入增长。静默截断会丢失审计事实，保留 tuple 则无法满足容量要求。

契约 2.4.0 的 `ResearchDatasetEvidenceManifest` 固定承诺六条 evidence streams。2.3.0 与 2.4.0 的已发布 manifest、证据字节和哈希回放必须保持原样。`PointInTimeSelection` 是既有 v2 输出契约，含完整 heads tuple；本决定不改变该类型或其调用语义。

## 决策（Decision）

为契约 2.5.0 增加 PIT 冲突证据流，使用 ADR-0077 的 bounded、content-addressed evidence tree 机制。新增的 `pit_conflicts` stream 对每个冲突 evaluation 按规范顺序写入全部 maximal-head 记录，不在任何 v3 结果或 Dataset manifest 行内嵌完整 heads 集合。每条记录绑定 PIT 规则身份（规则名、版本与 hash）、observation key、simulation time、knowledge cutoff、该 evaluation 的 head 总数、从零开始的 ordinal 和 revision ID。规范顺序为 `(rule identity, observation_key, simulation_time, revision_id)`；同一 evaluation 的 ordinal 连续且 revision ID 按现有 canonical ID 顺序递增。

v3 的冲突结果只保留固定大小的 PIT evaluation 身份、head 数、stream 根引用与记录数。构建器必须将本次冲突的所有 heads 排入 stream 后才返回 fail-closed 的冲突结果；不得选取 head，不得只保留前 N 个，也不得继续产出该冲突 key 的数据集行。根引用采用固定大小的 `EvidenceStreamRef`，可由显式 reader 完整重放和核验。旧 v2 仍返回既有 `PointInTimeSelection.maximal_heads` tuple。

PIT infrastructure 的 sorted-run 实现必须使用调用方显式提供的 ADR-0077 `RunLimits` 与 merge fan-out；每条 run 记录只含一个 revision ID。reader 按序读取全部 heads，并校验对象 key、hash、大小、结构和记录数；不得截断、采样或因冲突数量拒绝记录。部分 run 写入后失败时，已发布对象保持不可引用的 orphan，不自动删除；清理只能走显式 maintenance。

Dataset v3 的 2.5.0 manifest 增加第七个 `pit_conflicts` stream ref。成功构建的数据集该流必须为空；verifier 必须重放并确认该流为空。冲突时 Dataset 构建失败，不提交 dataset manifest；冲突结果携带已完成的 stream root/count 供诊断读取，写出的不可变对象按 ADR-0077 的 orphan 规则保留。失败构建不把部分 dataset manifest 当作提交点。

版本规则：

1. `CONTRACT_SCHEMA_VERSION` 提升到 `2.5.0` 并登记为已发布版本。
2. `EvidenceStream.PIT_CONFLICTS`、其记录模型和新 manifest 形状仅允许在 2.5.0 及之后的信封中出现；旧 envelope 遇到新 enum 值或模型字段必须拒绝。
3. 2.3.0 / 2.4.0 manifest 继续要求原六条 streams，按原 envelope 版本验证、读写与复算原哈希；不得回填空的第七条 stream。
4. v2 `PointInTimeSelection` 与 legacy dataset/report 持久形状保持不变。

本 ADR 不声称 E1-CAP-1 通过。端到端内存门仍须覆盖 PIT 单 key 图计算、冲突 evidence 写入和重放、Dataset / Quality report 接线及物理存储读取。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A：完整有序 heads 外排到内容寻址 evidence stream，v3 仅返回 root/count | 保留全部审计事实；结果大小固定；沿用 ADR-0077 reader / verifier 结构 | 新增一个 stream、记录模型及 2.5.0 兼容分支 | **选择**：唯一同时满足无损、可重放和有界结果的方案 |
| B：发现第二个 maximal head 即停止，只返回冲突类型 | 内存最少，实现较小 | 不能提供完整竞争 heads 证据，改变现有冲突诊断语义 | 拒绝：无业务规则授权丢弃其余冲突事实 |
| C：保留完整 tuple | 与 v2 形状一致 | tuple 本身无上限，违反 ADR-0077 工作集边界 | 拒绝：无法满足已接受的容量目标 |

## 后果（Consequences）

- 正面：v3 冲突诊断与证据读取保持完整；单个 PIT 结果只携带固定大小的 stream 引用。
- 负面 / 代价：契约、evidence reader/writer、Dataset v3 manifest builder/verifier 与失败结果路径都需支持新增 stream；契约升级到 2.5.0。
- 需要迁移的内容：无。已存 2.3.0 / 2.4.0 manifest 不变；新写入的 Dataset v3 使用 2.5.0。
- 对复现性的影响：v2 逐位兼容；2.3.0 / 2.4.0 继续按发布版本回放；2.5.0 新增完整冲突记录的确定性顺序与根身份。

## 合规检查

- [x] 不破坏已冻结契约；只增加版本化的 2.5.0 形状
- [x] 不修改 Validation Constitution
- [x] Domain 层仍无具体技术依赖；内容寻址实现留在 infrastructure
- [x] Research / Application Plane 边界不变

## 参考

- [ADR-0077：Bounded Research Dataset Evidence](0077-bounded-research-dataset-evidence.md)
- [ADR-0093：Bounded, versioned quality report evidence streams](0093-bounded-quality-report-evidence.md)
- [PIT bounded conflict output review](../reviews/2026-09-29-pit-bounded-conflict-output-decision.md)
