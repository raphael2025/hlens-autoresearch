# ADR-0107: E1 有界工作集第三轮与容量测量范围

| 字段 | 值 |
|---|---|
| 状态 | **Rejected**（2026-10-02，PM）：未在 `phase/1` 接受；2026-10-01 的分支版"Accepted"不构成授权。被 E1-CAP-1 正式矩阵结果与 ADR-0108 取代，见文末"不接受的理由" |
| 日期 | 2026-10-01 起草；2026-10-02 拒绝 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令 |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 1（E1-CAP-1） |
| 影响范围 | `infrastructure/catalog/`、`infrastructure/revision/`、`infrastructure/parser/`、`infrastructure/canonical/`、`infrastructure/universe/`、`infrastructure/dataset/`、`infrastructure/quality/`、`infrastructure/pit/`、`infrastructure/streaming/`、`infrastructure/tools/`；不改 `core/` |
| 是否破坏兼容 | 否（持久化格式、manifest、规则哈希与 run 格式不变） |

## 背景（Context）

ADR-0100 §6 之后，静态审计确认归档解析、PIT v3 单 key 路径、Dataset v3 证据与校验、Universe cursor 已基本有界，但仍有以下持有项威胁 32 MiB 门槛（门槛不变，H3）：

- A1：扫描批 65,536 行并整批 `to_pylist()`（约 18 处）；
- A2：热路径为取 head 而调用 `load_table`，PyIceberg 每次物化完整 TableMetadata，随 snapshot 数 H 增长；
- A3：Universe v3 对每个 (symbol, instant) 调用 `listing_at`，每次整表重证 exchangeInfo 历史（含 O(H²) 去重）；
- A4：25,000 行微批整体转 dict 并构造 pydantic 对象；
- A5：解析 spool 与持久化校验的临时文件未使用注入的 scratch 目录；
- A6 / A7：chunk 提交与校验同时持有约 4 份行 dict；quality manifest 查询走高层 planner；
- A8 / A9：normalizer 公共 `verify_unit` 整体物化；摄取返回值随微批数线性增长；
- A10：容量探针的基础阶段仍在跑 v2 物化路径；
- A11：exchangeInfo / REST body 整体读取（上限 64 MiB）。

## 决策（Decision）

1. **测量范围**：E1-CAP-1 只测量 v3 有界路径（与 ADR-0077 §8.3 一致）。v2 物化路径不在测量范围内，也不再被新入口使用；容量探针的基础阶段改测 v3（`iter_bounded` + `QualityReporterV3`），并逐阶段记录 RSS 与 cgroup 采样。exchangeInfo / REST 采集阶段（A11）不在 E1-CAP-1 范围内，作为采集侧单独风险记录，本轮不改。
2. **共享切片**：新增 `infrastructure/streaming/arrow_rows.py` 的 `iter_batch_rows(batch, step)`，所有 E1 路径的整批 `to_pylist()` 改为切片转换；扫描批行数改为 adapter 构造参数（工程参数，不是验证阈值）。
3. **有界 head / history**：catalog adapter 新增 `head_snapshot_id(table)` 与基于有界 pin 的 history 流，不实例化 `snapshots` 列表；热路径改用它们。PyIceberg 提交本身的 metadata 读写策略（合并微批、snapshot 过期）**先测 H 梯度再决定**，本 ADR 不改。
4. **有界 `listing_at`**：每次 Universe cursor walk 只证明一次 listing 历史（复用 `verify_table_bounded` / `ListingDeriver.verify_bounded`），把证明结果写入 SQLite scratch 点查索引（ADR-0097 模式，scratch 由调用方注入），`listing_at` 对索引查询。结果必须与旧 `listing_at` 逐点相等（等价测试）。
5. **微批与校验**：微批内按子块（调用方给定行数）转换与校验；`row_commits` 折叠为计数、首尾 snapshot 与 digest；chunk 比对改用 Arrow 比较或按 row_ordinal 流式指纹；quality manifest 查询改走 `scan_column_batches`；normalizer 公共 `verify_unit` 改为 sink API。
6. **scratch**：解析 spool 与持久化校验临时文件一律写入注入的 scratch 目录，不回退系统临时目录（ADR-0077 / 0097）。
7. **分支处置**：15 个 E1 旧分支经审计均已被 `main` 取代或只是 WIP 快照；移植 `codex/e1-replay-25-tests` 的 4 个回放测试（手工摘取），其余存归档 ref 后删除。`codex/adr-bounded-quality-reports` 中的 ADR 草案与 main 已接受的 ADR-0094 撞号，不移植；若以后需要 `qgap_output_snapshot_id` 设计，另立 ADR。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 把 v2 路径纳入测量并改造 | 全面 | 违背 ADR-0077 §8.3，v2 已停止新建 | 不必要 |
| 先改 PyIceberg 提交策略 | 可能消除 O(H) | 影响面大、需实测支撑 | 先测后定 |

## 后果（Consequences）

- 正面：E1-CAP-1 的测量对象与有界声明一致；剩余已知持有项全部有对应改造。
- 负面 / 代价：约 2000 行代码与测试，跨多个热文件；是否通过 32 MiB 仍以实测为准。
- 对复现性的影响：无；所有输出逐位不变（等价测试固定）。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 32 MiB 门槛或任何验证规则（H3）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变


## 不接受的理由（2026-10-02，PM）

1. **测量范围已由协议固定**：E1-CAP-1 由 `infrastructure.tools.normalizer_memory_probe` 的正式协议测量（M = 256，N = 10k / 100k / 500k，各 3 次，代码行与 `main` 一致），`main@50d6bb6` 上每 stage 增长 ≤ 31.2 MiB、数值 PASS（[记录](../reviews/2026-10-02-e1-cap1-main-50d6bb6.md)）。本 ADR §1 的"只测 v3"与 A1–A10 的大部分持有项，在该协议下或已不出现在测量路径上，或已被此后的 E1 收口（归档 spool 复用、Raw / Canonical 窗口复用）覆盖。
2. **未关闭的唯一原因已另行决定**：E1-CAP-1 未关闭的原因是 Iceberg metadata 随批次数线性增长；ADR-0108（一个逻辑单元一个 snapshot）直接处理它。本 ADR §3 的有界 head / history 只解决读、不解决写（ADR-0108 方案 D 已论证），分支 `feature/e1-catalog@7465736` 中对应函数没有调用方，不移植。
3. **§6 scratch**：解析 spool 改为使用注入的 scratch 目录已随 ADR-0108 实现落地（`RawRevisionStore` 把 `scratch_directory` 传给 `parse_archive_spooled`）。
4. **残余项不丢失**：A3（Universe v3 `listing_at` 每点整表重证）与 A11（exchangeInfo / REST body 整体读取）不在 E1-CAP-1 测量路径上，记为 Phase 1 风险 / 后续工作；若日后的测量或生产规模回填（D-META-AGE）证明需要，另立新 ADR，不复用本 ADR 编号。
5. §7 的分支处置已于 2026-10-01 执行（旧 E1 分支 tip 存于 `refs/archive/2026-10-01/`）。
