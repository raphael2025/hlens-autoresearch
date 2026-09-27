# ADR-0026: 为按天分区写入加入 PyIceberg 官方 extra `pyiceberg-core`（D-32）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-24，Codex 依 Raphael 持续授权裁决，选择方案 A）** |
| 日期 | 2026-09-24 |
| 决策者 / 批准者 | Codex（依据 Raphael 对技术栈、架构与文档的持续授权；独立审查 C3 `c193918` 后裁决） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文（批次 D32，docs-only） |
| 相关 Phase | Phase 1 |
| 影响范围 | Infrastructure（依赖清单） |
| 是否破坏兼容 | 否：不改任何契约、Schema、表名、分区、写入语义或代码 |
| 补充 | 补充 [03-data.md](../architecture/03-data.md) §6.1 的最小直接依赖清单；[ADR-0021](0021-phase1-local-data-infrastructure.md)、[ADR-0023](0023-bitemporal-revision-data.md) 正文不回写、继续有效 |

## 背景

03-data.md §7.1 已冻结八张首切片表，其中四张以 `day(...)` 为初始分区：
`raw.binance_spot_agg_trades`、`raw.binance_spot_klines_1m`、`canonical.trades`、`canonical.bars_1m`。
§6.1 冻结并由 A3a 锁定的直接依赖是 `pyiceberg[pyarrow,sql-postgres]`。

C3（`c193918`）在真实 PostgreSQL catalog 上发现：锁定的 PyIceberg 0.12 中 `DayTransform.pyarrow_transform()`
通过 `_try_import("pyiceberg_core", extras_name="pyiceberg-core")` 调用其官方 optional extra `pyiceberg-core`；
缺少该 extra 时，对上述四张表的 append 在写入前抛出 `NotInstalledError`。C3 的 PostgreSQL 测试证明：
四张表可以建表、核对与分区演进，失败的写入不改变表状态，但写入本身不可能完成；相应的 5 条 PostgreSQL 路径以 `xfail` 标注。
这使验收 #9 的 batch 幂等 commit 在四张表上无法成立，也阻塞 D / E 批次的 trades 与 bars 落地。

本 ADR **不**表示 C3 已验收；C3 仍待 Codex 复核。

## 决策

1. **选择方案 A**：把现有直接依赖由 `pyiceberg[pyarrow,sql-postgres]` 改为
   `pyiceberg[pyarrow,pyiceberg-core,sql-postgres]`。这仍是同一个顶层 PyIceberg 依赖，只增加其官方 extra；
   `pyiceberg-core` 的精确版本由 PyIceberg 0.12 自身声明的约束解析并写入 `uv.lock`，不作为独立顶层依赖。
2. **保持不变**：表名、列级 Schema、初始 partition spec（含 `day(...)`）、ADR-0023 §7 的写入路径
   （有界 `pyarrow.Table` microbatch + 稳定 batch id，经 PyIceberg append / commit）与 Adapter 边界全部不变；
   `core/` 仍不依赖任何具体技术（H7）。
3. **实施分工**：Cursor 修改 `pyproject.toml` 并重新锁定 `uv.lock`；随后 Claude 把 C3 中因 D-32 标注的 5 条 `xfail`
   转为正常通过（删除 D-32 分支，不放宽任何断言）。二者完成并经 Codex 复核后，C3 才能验收。
4. **授权边界**：本决定只授权上述一个 extra。不授权其它依赖、系统软件安装、表重建、D0 开放或外网数据访问。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 加入官方 extra `pyiceberg-core` | 冻结的表、分区与写入语义全部不变；使用 PyIceberg 自身声明的实现路径 | 多一个传递依赖（含原生扩展） | — |
| B 把 `day(...)` 改为 identity 日期列 | 不需要新依赖 | 改变 03-data.md §7.1 已冻结的 partition spec，并引入冗余日期列 | 改变冻结内容 |
| C 先写 Parquet 再 `add_files` 绕过 append | 不需要新依赖 | 改变 ADR-0023 §7 选定的写入路径与 batch 幂等语义 | 改变已接受的写入决定 |

## 后果

- 正面：四张按天分区的表可按原冻结设计写入；C3 验收与 D / E 批次的阻塞解除（待依赖锁定与测试转正后）。
- 负面 / 代价：依赖图增加 `pyiceberg-core`；PyIceberg 升级时须同时核对该 extra 的版本约束。
- 需要迁移的内容：无；已建的空表无需重建。
- 对复现性：`uv.lock` 固定 `pyiceberg-core` 版本，属于复现元组中的依赖锁；此前没有任何按天分区的数据写入，无旧结果受影响。

## 合规检查

- [x] 不修改任何 Domain Contract、Schema 或 Constitution
- [x] 不修改 03-data.md §7.1 冻结的表名与分区，不修改 ADR-0023 写入路径
- [x] Domain 层仍无具体技术依赖（H7）
- [x] 不授权系统安装、外网数据访问或 D0；不回写已 Accepted ADR 正文
- [x] 决定由 Codex 依 Raphael 持续授权作出，以本 ADR 书面记录

## 参考

- C3 提交 `c193918`；`tests/infrastructure/catalog/test_phase1_tables_postgres.py`（D-32 `xfail` 路径）
- [03-data.md](../architecture/03-data.md) §6.1 / §7.1；[ADR-0021](0021-phase1-local-data-infrastructure.md)；[ADR-0023](0023-bitemporal-revision-data.md) §7
- PyIceberg 0.12.0 包元数据：`Provides-Extra: pyiceberg-core`（`pyiceberg-core>=0.10.1,<0.11.0`）
