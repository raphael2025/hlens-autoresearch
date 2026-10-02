# ADR-0108: Canonical 提交粒度与 Iceberg 元数据增长

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-10-02） |
| 日期 | 2026-10-02 |
| 决策者 | 待定（Claude Code PM 或 Raphael） |
| 起草者 | Claude Code（PM），依据 2026-10-01/02 E1-CAP-1 正式探针与 profile |
| 相关 Phase | Phase 1（E1-CAP-1），影响所有后续写入 Canonical 的路径 |
| 影响范围 | Data / Infrastructure：`infrastructure/canonical/normalizer.py`、`infrastructure/catalog/iceberg_adapter.py`；可能涉及 ADR-0075 / 0076 的提交与重放语义 |
| 是否破坏兼容 | 视方案而定（推荐方案须对既有逐微批提交的历史保持只读重放兼容） |

## 背景（Context）

Normalizer 每个 microbatch（M = 256 行）向 `canonical.*` 提交一个 Iceberg snapshot，且项目不做 snapshot 过期（PIT 重放依赖完整历史，ADR-0023 / 0075）。PyIceberg 每次提交与每次 `load_table` 都解析并物化整份 `TableMetadata`（含全部 snapshot）。

实测（2026-10-02，`main@50d6bb6` 正式探针工作目录，只读加载）：一个 500k 行单元写完后 `canonical.trades` 有 1954 个 snapshot，metadata JSON 2.0 MiB，解析后 Python 对象 8.7 MiB、解析峰值 13.5 MiB；每 snapshot 约 4.5–7 KB，线性。正式探针中 `resume` stage（后半段提交，表最大）RSS 增量 10k 50.0 → 100k 61.1 → 500k 77.3 MiB，增长约 27 MiB，与"提交时新旧两份 metadata 并存"的量级一致。另一个成本：ADR-0075 的有界扫描每次遍历快照全部 manifest，每次提交新增一个 manifest，逐提交 read-back 的时间随 N 二次增长（500k `write_crash` 约 43–46 分钟）。

探针每个单元都从空表开始，因此**低估生产**：生产中 Canonical 表的 snapshot 跨所有单元、所有日期累积且永不过期。以每天每 symbol 约 2000 次提交估算，一个月即约 10 万 snapshot，单份 metadata 约 0.5 GB，与 32 MiB 工作集声明（E1-CAP-1）和可运行性根本冲突。ADR-0107（仅在分支）已把"提交本身的 metadata 读写策略"列为"先测 H 梯度再决定"；本 ADR 给出该梯度并请求决定。

## 待决定（Decision requested）

在保持 PIT 历史与崩溃恢复语义的前提下，使 Canonical 表的 snapshot / manifest 数量不随行数线性增长。

## 备选方案（Alternatives）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A（推荐） | **按单元一次提交**：microbatch 仍决定内存有界的处理与数据文件切分（每批写一个已暂存数据文件），但一个单元的全部数据文件在**一个** snapshot 中追加（多文件 fast_append）；崩溃时未提交的暂存文件为孤儿（与 ADR-0077 evidence 孤儿语义一致），重跑幂等 | snapshot 数 = 单元数（每天每 symbol 个位数），metadata 与 manifest 数随单元数而非行数增长；逐提交 read-back 变为每单元一次 | 断点续写粒度从微批变为单元（崩溃后整单元重做暂存，但暂存可内容寻址复用）；需改写 normalizer 计划 / 恢复 / 重放逻辑与批 id 语义；须对既有逐微批历史保持只读重放 |
| B | 每 k 个 microbatch 一次提交（k 为规则参数） | 改动较小 | 只把常数降 k 倍，仍线性增长，未消除根因 |
| C | snapshot 过期 + 外部历史账本 | metadata 有界 | 破坏按 snapshot 钉定的 PIT 重放与 manifest 绑定（ADR-0023 / 0075），否决 |
| D | 自研流式 / 外存 metadata 解析与写入，绕开 PyIceberg 物化 | 不改数据布局 | 需要重写 Iceberg 提交协议核心，风险与维护成本极高；ADR-0107 §3 的有界 head / history 只解决读，不解决写 |
| E | 维持现状，放宽 32 MiB | 无改动 | 违反 H3 / E1-CAP-1 门槛，且生产仍不可运行，否决 |

## 推荐

方案 A。实施前须先完成设计说明：批 id / 计划身份如何从"逐微批提交"迁移到"单元提交"、既有历史的只读重放兼容、暂存孤儿与幂等重跑、逐单元 read-back 的有界实现，以及对 PIT 选择器 `iter_bounded` 读取单元批的影响；并在实现后重跑 E1-CAP-1 正式矩阵，外加一个"表中已有大量历史 snapshot"的预填充场景（探针当前没有）。

## 若不决定的影响

E1-CAP-1 正式矩阵即使在单单元场景下通过，也不能代表生产规模；Phase 1 验收仍应视为被本问题阻断。

## 合规检查

- [x] 不改 32 MiB 门槛或任何验证规则（H3）
- [x] 不删除历史数据；方案 C 已否决
- [ ] 契约影响待设计说明确认（推荐方案预期不改 `core/` 契约，但改变 normalizer 批 id 语义需单独说明）
