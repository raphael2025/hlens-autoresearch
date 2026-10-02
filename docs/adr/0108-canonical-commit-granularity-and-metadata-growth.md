# ADR-0108: Canonical 提交粒度与 Iceberg 元数据增长

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02） |
| 日期 | 2026-10-02（Proposed 与 Accepted 同日） |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 在主会话的直接指令「你来决定ADR-0108 然后跟新文档」 |
| 起草者 | Claude Code（PM），依据 2026-10-01/02 E1-CAP-1 正式探针与 profile |
| 相关 Phase | Phase 1（E1-CAP-1），影响所有后续写入 Raw 元素表与 Canonical 的路径 |
| 影响范围 | Data / Infrastructure：`infrastructure/canonical/normalizer.py`、`infrastructure/revision/store.py`（归档元素写入）、`infrastructure/catalog/iceberg_adapter.py`、PIT 证明读取；`core/` 不变 |
| 是否破坏兼容 | 否：Canonical / Raw 行内容按位不变；既有逐微批提交的历史保持只读可读、可证明、可重放 |

## 背景（Context）

Normalizer 每个 microbatch（M = 256 行）向 `canonical.*` 提交一个 Iceberg snapshot，且项目不做 snapshot 过期（PIT 重放依赖完整历史，ADR-0023 / 0075）。PyIceberg 每次提交与每次 `load_table` 都解析并物化整份 `TableMetadata`（含全部 snapshot）。

实测（2026-10-02，`main@50d6bb6` 正式探针工作目录，只读加载）：一个 500k 行单元写完后 `canonical.trades` 有 1954 个 snapshot，metadata JSON 2.0 MiB，解析后 Python 对象 8.7 MiB、解析峰值 13.5 MiB；每 snapshot 约 4.5–7 KB，线性。正式探针中 `resume` stage（后半段提交，表最大）RSS 增量 10k 50.0 → 100k 61.1 → 500k 77.3 MiB，增长约 27 MiB，与"提交时新旧两份 metadata 并存"的量级一致。另一个成本：ADR-0075 的有界扫描每次遍历快照全部 manifest，每次提交新增一个 manifest，逐提交 read-back 的时间随 N 二次增长（500k `write_crash` 约 43–46 分钟）。

探针每个单元都从空表开始，因此**低估生产**：生产中 Canonical 表的 snapshot 跨所有单元、所有日期累积且永不过期。以每天每 symbol 约 2000 次提交估算，一个月即约 10 万 snapshot，单份 metadata 约 0.5 GB（此估算按探针的 M = 256；按默认 M = 25,000 约小两个数量级，但仍随行数线性增长、永不过期，结论不变），与 32 MiB 工作集声明（E1-CAP-1）和可运行性根本冲突。ADR-0107（仅在分支）已把"提交本身的 metadata 读写策略"列为"先测 H 梯度再决定"；本 ADR 给出该梯度并请求决定。

## 决策（Decision）

采用**方案 A：一个逻辑单元 = 一个 Iceberg snapshot**，并把同一规则扩展到 Raw 归档元素写入。microbatch 只决定处理与数据文件切分，不再决定提交粒度。

1. **规则。** E1 写入链上，每张表的 snapshot 数 = 写入的逻辑单元数，与单元行数 N、microbatch 大小 M 无关：
   - Canonical：一次 `normalize_unit`（一个 Raw source revision 的一个数据类型）= `canonical.*` 上恰好一个 snapshot；
   - Raw 归档：一个归档 revision 的全部元素行 = 元素表上恰好一个 snapshot（归档 source 行的提交保持不变）；
   - REST response 本来就按页提交，不受影响。
2. **机制：只加在 infrastructure，不改契约。** 沿用 ADR-0075 的先例，在 Iceberg adapter 上增加一个 infrastructure 内部的"暂存单元提交"能力。冻结的 `core/contracts/catalog.py`（`CommitRequest` / `CatalogAdapter.commit_batch`）、契约版本 2.5.0 与 Schema 数量都不变。具体做法：
   - 每个 microbatch 仍是有界的 `pyarrow.Table`（`03-data.md` §7.2），各自写成表 location 下的一个暂存 Parquet 数据文件，文件名按内容寻址；
   - 全部数据文件在一次多文件 fast append 中提交，期望父 snapshot 照常检查；
   - snapshot summary 写入单元 batch id、单元行数，以及整个单元的指纹。指纹沿用 `hlens.pyarrow-batch-sha256@1.0.0` 帧格式，用现有的 `fingerprint_run` 有界流式计算，结果与"整单元拼成一个 Table 再算"按位相同；
   - 幂等语义与 `commit_batch` 相同：同表同 batch id、同指纹 = 已提交，不同指纹 = `BatchConflict`。
3. **batch id 与计划。** 新写入的单元使用单元级 batch id：编码 normalizer / 版本、source revision 和单元行数，不再含 microbatch 序号。microbatch 大小写进 snapshot summary 供核对，不再参与"已提交前缀"的计划恢复。新写入的单元只有两种状态：完全未提交，或整单元已提交；"已提交前缀"状态从此只可能出现在旧历史里。
4. **崩溃与恢复。**
   - 暂存文件写完、提交之前崩溃：没有任何 snapshot 引用这些文件，读者不可见。它们是孤儿文件（与 ADR-0077 evidence 孤儿语义一致），只能由日后的显式 maintenance 清理（`03-data.md` §7.2）。
   - 重跑时重新暂存（内容寻址，可复用），再提交。
   - 提交成功但返回前崩溃：重跑得到"已提交"并照常证明。
5. **read-back。** 逐提交 read-back 改为每个单元一次：在单元 snapshot 上按 ADR-0075 有界扫描逐窗口核对（逐行精确、每行一次、revision id 唯一），工作集仍以 microbatch 为界。这也消除了写入耗时的二次增长。
6. **PIT 与证明。** PIT / `verify_unit` 证明的工作单位仍是 microbatch 窗口（单元内按 `arrival_seq` 的秩切片），只是改在单元唯一的 snapshot 内定位，不再逐批找 snapshot；内存仍以 microbatch 为界。
7. **旧历史兼容（只读）。** 已按逐微批提交的单元保持可读、可证明、PIT 可重放，结果按位不变。旧单元若只提交了前缀，仍判为 `CanonicalUnitIncomplete`，只能沿旧的逐微批路径补齐。一个单元内不得混用两种提交布局，混用即判为完整性错误、失败关闭。
8. **规则版本。** 只要实现证明同一 Raw 在新旧两种布局下产出的 Canonical 行按位相同，normalizer 内容规则 `hlens.canonical.binance-spot.normalizer@1.0.0` 就不升版；提交布局由 snapshot summary 中的独立布局标识区分。若行内容有任何差异，必须停下另立 ADR。
9. **验收（实现后才能关闭 E1-CAP-1）。**
   - (a) 结构断言：新写入的单元在 Canonical 与 Raw 元素表上各只产生 1 个 snapshot，与 N、M 无关；
   - (b) 两种布局输出按位一致的测试；旧布局读取 / 证明 / 前缀补齐的回归测试；
   - (c) 崩溃注入：暂存后、提交前崩溃不可见且可重跑；提交后、返回前崩溃得到幂等结果；
   - (d) 在与 `main` 一致的提交上重跑 E1-CAP-1 正式矩阵：M = 256，N = 10k / 100k / 500k，每档 3 次，**32 MiB 门槛不变**；
   - (e) 新增**预填充历史场景**：表中预先有 K ∈ {0, 1000, 3000} 个已提交小单元，再测同一矩阵。分别报告"随 N 的增长"（门槛判定项）和"随 K 的绝对成本"（只记录，供后续决定）；
   - (f) 全仓门禁（pytest / ruff / format / mypy）全绿。
10. **不在本 ADR 范围内。** Research Dataset v3 chunk 写入（ADR-0077，每个 chunk 一个 snapshot）同样会随行数增加 snapshot，单独评估，不并入本批。snapshot 过期依然禁止。

## 备选方案（Alternatives）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A（**采纳**） | **按单元一次提交**：microbatch 仍决定内存有界的处理与数据文件切分（每批写一个已暂存数据文件），但一个单元的全部数据文件在**一个** snapshot 中追加（多文件 fast_append）；崩溃时未提交的暂存文件为孤儿（与 ADR-0077 evidence 孤儿语义一致），重跑幂等 | snapshot 数 = 单元数（每天每 symbol 个位数），metadata 与 manifest 数随单元数而非行数增长；逐提交 read-back 变为每单元一次 | 断点续写粒度从微批变为单元（崩溃后整单元重做暂存，但暂存可内容寻址复用）；需改写 normalizer 计划 / 恢复 / 重放逻辑与批 id 语义；须对既有逐微批历史保持只读重放 |
| B | 每 k 个 microbatch 一次提交（k 为规则参数） | 改动较小 | 只把常数降 k 倍，仍线性增长，未消除根因 |
| C | snapshot 过期 + 外部历史账本 | metadata 有界 | 破坏按 snapshot 钉定的 PIT 重放与 manifest 绑定（ADR-0023 / 0075），否决 |
| D | 自研流式 / 外存 metadata 解析与写入，绕开 PyIceberg 物化 | 不改数据布局 | 需要重写 Iceberg 提交协议核心，风险与维护成本极高；ADR-0107 §3 的有界 head / history 只解决读，不解决写 |
| E | 维持现状，放宽 32 MiB | 无改动 | 违反 H3 / E1-CAP-1 门槛，且生产仍不可运行，否决 |

## 后果（Consequences）

- Canonical 与 Raw 元素表的 snapshot 数、metadata 大小和 manifest 数，不再随行数增长，只随单元数增长。E1-CAP-1 所测的"随 N 增长"一项可以在结构上被证明有界。
- 断点续写粒度从 microbatch 变为单元：崩溃后要重做整个单元的暂存，但暂存可按内容复用，且单元工作集仍有界。
- normalizer 的计划 / 恢复 / 重放逻辑和 Raw store 的元素写入要改写；旧布局读取路径必须保留并测试。
- **残余问题（不由本 ADR 解决，记为 D-META-AGE）：** metadata 仍随表的历史长度（单元数 K）线性增长，PyIceberg 每次 `load_table` 仍会物化全部 snapshot。粗估：Phase 1 两个 symbol 若都用日归档覆盖 2017-08-17 至今，每张 Canonical 表约 6,700 个单元，metadata JSON 约 30–47 MB，解析后约 130–200 MiB（按实测解析 / JSON 约 4.35 倍）；历史段改用月归档则约 220 个单元。这是生产可运行性问题，不是 E1-CAP-1 随 N 的判定项，但不能隐去。候选方向：历史段用月归档单元；为回填设计多单元提交组；评估 catalog 侧 metadata 策略。等 9(e) 的 K 轴数据出来后另立 ADR 决定。

## 合规检查

- [x] 不改 32 MiB 门槛、E1-CAP-1 协议或任何验证规则（H3）；新增的预填充场景只会更严格，不会更宽松
- [x] 不删除历史数据，不过期 snapshot；方案 C 已否决（H6）
- [x] 不改 `core/` 契约（H1）：暂存单元提交只在 infrastructure 内；实现中若发现必须改契约，停下另立 ADR
- [x] 不削弱现有测试（H4）：旧布局测试保留，新布局另加测试

## 参考

- [E1-CAP-1 正式探针记录](../reviews/2026-10-02-e1-cap1-main-50d6bb6.md)
- ADR-0023 §7（写入约束）、ADR-0075（有界扫描）、ADR-0076（有界规范化结果）、ADR-0077（evidence 孤儿语义）
- `docs/architecture/03-data.md` §7.2
