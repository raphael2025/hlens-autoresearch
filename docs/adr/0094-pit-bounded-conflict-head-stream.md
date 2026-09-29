# ADR-0094: PIT bounded conflict heads as a content-addressed run

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-29 |
| 决策者 | Codex，依当前项目目标授权的工程决策权 |
| 起草者 | Codex |
| 相关 Phase | Phase 1 — Market Representation |
| 影响范围 | Infrastructure / Data |
| 是否破坏兼容 | 否；仅改变 ADR-0077 bounded v3 infrastructure 输出 |

## 背景（Context）

[ADR-0077](0077-bounded-research-dataset-evidence.md) 要求 PIT v3 对单 key 历史使用固定工作集。
当前 `PointInTimeSelection.maximal_heads` 是无长度上界的完整 tuple。即使图遍历改为外存，直接返回该 tuple
仍会按冲突 head 数增长。旧 v2 的契约与持久化 replay 必须逐位保持不变。

## 决策（Decision）

1. 保留 v2 `PointInTimeSelection.maximal_heads` tuple、`select()` 语义及已持久化 replay，均不变。
2. `PitSelector.iter_bounded()` 的 v3 infrastructure 输出改用固定大小 selection DTO。`selected` 保留唯一
   `selected_revision_id`；`absent` 不含 head；`conflict` 提供完整 head 数以及按 revision ID 升序写出的
   content-addressed `RunRef`，不在返回对象中保留完整 tuple。
3. 冲突 head run 复用 ADR-0077 的 sorted-run 物理格式、调用方给定的 `RunLimits` 和 merge fan-out；每条记录
   只含一个 revision ID。公开 reader 按序读取全部 heads，并验证每个 run 对象的键、hash、大小、结构和记录数。
   不截断、不采样、不因冲突数量拒绝记录。
4. `DatasetBuilder` 按 conflict count fail closed，无需物化 heads；诊断调用方可以显式打开 run reader。
   一次 evaluation 的 selected / absent / conflict 状态压缩仍须按 v2 的相邻相等规则保持完全一致。
5. 部分 run 写入后失败时，已发布对象是不可引用的 orphan；不删除、不参与 selection。该语义沿用 ADR-0077 §9，
   后续清理只能走显式 maintenance。
6. 本 ADR 只决定冲突结果形状，不代表单 key graph/reachability 已有界，也不代表 E1-CAP-1 通过。完整保留
   uniqueness、ownership、声明边、时间顺序、环检测、cutoff 与可用时间语义仍是实现验收要求；32 MiB 门槛不变。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A：bounded content-addressed run | 保留完整 heads、返回常量大小引用、复用 ADR-0077 写读原语 | 消费者需显式打开 reader | 选择；同时满足完整诊断与固定返回工作集 |
| B：第二个 head 即 fail closed | 最简单、工作集最小 | 丢掉其余 competing-head 事实，改变诊断完整性 | 不选；不符合完整证据记录目标 |
| C：返回完整 tuple | 现有接口直观 | 冲突时仍 O(N) | 不选；违反 ADR-0077 有界目标 |

## 后果（Consequences）

- 正面：冲突事实全部保留，bounded 返回对象不随 head 数增长；legacy v2 完全隔离。
- 代价：读取诊断需提供 storage 与 run limits；run 对象数量和存储量随冲突 head 数增长，必须由 ADR-0077 的完整测量覆盖。
- 契约与报告：不改 core Contract、Schema 或 report hash；v3 Dataset 冲突仍在提交前失败，不产生可用 manifest。
- 复现性：sorted-run 对象按内容寻址，顺序与身份确定。

## 合规检查

- [x] 不改变已发布 v2 契约和持久化身份
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- [ADR-0077](0077-bounded-research-dataset-evidence.md) §§6、9、10
- [PIT bounded conflict output decision packet](../reviews/2026-09-29-pit-bounded-conflict-output-decision.md)
