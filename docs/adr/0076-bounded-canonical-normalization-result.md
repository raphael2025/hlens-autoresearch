# ADR-0076: Bounded Canonical normalization result

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-28 |
| 决策者 | Codex（依 Raphael 2026-09-28 授权决定） |
| 起草者 | Codex |
| 相关 Phase | Phase 1 — Market Representation |
| 影响范围 | Infrastructure / Data |
| 是否破坏兼容 | 是（`infrastructure.canonical` 的结果 DTO） |

## 背景（Context）

E1-CAP-1 的权威口径把 normalize 调用的完整进程工作集计入 32 MiB 增量上限，覆盖 N=500,000。`CanonicalUnitNormalized.revision_ids` 当前返回所有 ID 的 tuple。Canonical ID 为 `crev1-` 加 SHA-256 十六进制，共 70 个 ASCII 字符；500,000 个 Python 字符串及 tuple 引用本身的结构估算约 60 MiB，尚未计入构造时的 list、批次摘要与其它工作集。降低内部副本不能消除返回 tuple 本身的线性占用。

仓库内生产工具只读取 `revision_ids` / `commits` 的长度或 replay 摘要，没有应用层、core 或 Domain 消费者。`CanonicalUnitNormalized` 是 `infrastructure.canonical` 导出的基础设施 DTO，不是冻结的 core contract。显式消费完整 ID 的需求仍须支持，且现有顺序是 Raw position 升序。

## 决策（Decision）

1. `normalize_unit()` 的默认结果不得物化全部 revision IDs 或每批 `BatchCommit` DTO。结果改为固定大小摘要：`revision_count`、`batch_count`、`replayed_batch_count`、由批次数推导的 `replayed`、原有 unit 身份字段与 block / ready 时间。空单元保持零计数与现有 replay 判定。
2. `CanonicalNormalizer` 提供显式 `iter_revision_ids(result)` 接口。它以 Raw position 升序逐个产出 ID，使用一个 `microbatch_rows` 窗口处理，并复用现有 Raw 证明、已提交 Canonical 一致性与身份计算规则。它不在结果对象上创建隐式惰性属性，不保留临时文件句柄，也不把全量输出从容量口径排除。
3. 流式 iterator 必须在自然耗尽、显式关闭、读取错误及调用方提前停止时释放 survey positions 与底层 readers。调用方需要完整结果时必须显式迭代；默认 normalize 路径不持有这些 ID。
4. 删除仅供结果展示的 `commits` tuple；`batch_count` 与 `replayed` 保留仓库内已使用的摘要语义。每批快照、ID 身份算法、追加顺序、失败拒绝条件与 Iceberg 持久化格式不变。
5. 此接口变化只处理 Canonical API 返回对象，不改变 core/domain contract、Raw / Canonical table schema、revision identity、验证规则或 E1-CAP-1 的 32 MiB 门槛。任何需要访问完整 ID 的调用方必须改用显式 stream；不得通过只量测消费者不持有结果的方式宣称容量通过。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 保留完整 tuple，仅移除中间副本 | API 不变 | N=500,000 时仅 ID 与 tuple 引用结构即超过 32 MiB | 与完整工作集门槛冲突 |
| 返回隐式惰性 Sequence 或结果内临时文件句柄 | 表面上保留索引 / 遍历体验 | 隐藏重复 IO、快照来源与文件生命周期；调用方持有时间不可控 | 无法给出明确、可复现的资源边界 |
| 有界摘要 + 显式有序 iterator | 默认结果固定大小；需要全部 ID 时仍能逐项消费 | 需要重复读取 / 验证 Raw 与 Canonical；调用方 API 改动 | 保留必需语义并使完整读取的资源成本显式 |

## 后果（Consequences）

- 正面：normalize 的默认返回值不随 unit 行数或微批数量持有输出 tuple / DTO；仓内目前只用计数与 replay 摘要的调用方可保持其业务目的。
- 负面 / 代价：显式读取所有 ID 会重复做有界窗口验证与计算，产生额外 IO 与 CPU；外部直接依赖旧属性的调用者必须迁移。
- 需要迁移的内容：`infrastructure/tools/` 中的容量 / capability 工具改读摘要字段；ID 内容测试改用显式 iterator；结果 DTO 文档同步。
- 对复现性的影响：ID 和行序、写入与 commit 语义不变；默认结果只记录摘要。需要完整身份清单的消费者必须显式迭代并自行持久化其输出。

## 合规检查

- [x] 不破坏已冻结契约；只变更 Infrastructure 结果 DTO，并记录兼容性变化
- [x] 不修改 Validation Constitution / Profile / 验收阈值
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- `docs/reviews/2026-09-27-e1-review.md`
- `docs/reviews/2026-09-28-e1-cap1-design-reconciliation.md`
- `infrastructure/canonical/normalizer.py`：`CanonicalUnitNormalized`、`CanonicalNormalizer._write()`
- 仓内消费者：`infrastructure/tools/dnet_capability_run.py`、`capacity_probe.py`、`normalizer_memory_probe.py`
