# ADR-0089: State 物理表 `state.states` 与显式存储入口

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 项目授权 |
| 相关 Phase | Phase 2 — Market State Engine |
| 影响范围 | Infrastructure：新增 `state.states` Iceberg 表、运行存储、文件型 State artifact store 与独立创建命令；不改契约、Schema、Phase 1 表或自动启动流程 |
| 是否破坏兼容 | 否：既有 State DTO 和 `state_table` 的 9 列逻辑投影不变；无契约变化 |
| 实施状态 | IMPLEMENTED / CHECKS_NOT_RUN；不在真实 Catalog 建表 |
| 前置 | [ADR-0035](0035-state-provider-contract.md) State DTO / 逻辑表；[ADR-0056](0056-event-table.md) additive Iceberg 表模式；[ADR-0066](0066-explicit-event-table-operator.md) 显式 `--apply` 操作入口 |

## 背景

ADR-0035 §4 已定义 `infrastructure/state/table.py` 的 `state_table(spec, request, result, descriptor)`：每个评估时刻一行，包含 State 身份与结果值，但当时未批准物理 `state.*` 表。State 运行需要可绑定的 catalog snapshot；本地 artifact store 则用于按 `result_hash` 留存和取回完整 `StateResult`。两者互补，均采用只追加、内容可复核的语义。

## 决定

### 1. 表与定义

- 新增 `state.states`，`definition_id = state.states`，定义版本 `1.0.0`。
- 使用既有 `hlens.pyarrow-batch-sha256@1.0.0` fingerprint、`write.parquet.compression-codec=zstd`、Iceberg format version 2 与 adapter 强制的零 commit retry。
- 定义位于 `infrastructure/state/table_definition.py`，导出 `STATE_STATES`、`PHASE2_TABLES`、`PHASE2_REGISTRY` 与显式 `ensure_state_tables(adapter)`。State 表不加入 `PHASE1_TABLES` / `PHASE1_REGISTRY`；调用方自行组合 registry。
- `state.states` 不自动创建、不接入 Phase 1 provisioning、应用启动或 worker。

### 2. 列与身份

每行保留 `state_table` 既有 9 列原名、顺序与含义，并追加 run envelope。字段 ID 固定如下：

| ID | 列 | 类型 | 必填 | 含义 |
|---:|---|---|---|---|
| 1 | `state_ref` | string | 是 | `StateSpec.ref` 的规范字符串 |
| 2 | `spec_hash` | string | 是 | `StateRequest.spec_hash` |
| 3 | `provider` | string | 是 | `StateResult.provider` |
| 4 | `request_hash` | string | 是 | `StateResult.request_hash` |
| 5 | `result_hash` | string | 是 | `StateResult.result_hash`；run identity |
| 6 | `evaluation_time` | timestamptz | 是 | State 评估时刻，UTC 微秒 |
| 7 | `state` | string | 否 | 状态标签；`null` 表示不可计算 |
| 8 | `inputs_used` | long | 是 | `StateValue.inputs_used` |
| 9 | `latest_input_time` | timestamptz | 否 | `StateValue.latest_input_time` |
| 10 | `provider_hash` | string | 是 | State Provider descriptor 的内容哈希 |
| 11 | `evaluation_index` | long | 是 | 该结果中按 evaluation time 严格升序的 0 起序号 |
| 12 | `evaluation_count` | long | 是 | 该 StateResult 的评估值总数 |
| 13 | `run_schema_version` | string | 是 | 该 StateResult / StateValue 的记录契约信封版本 |

字段 1–9 与 `STATE_TABLE_SCHEMA` 逐项一致。`result_hash` 覆盖 request hash、provider、provider hash 和按序的全部 `StateValue`。运行块使单个 snapshot 中的行可按原记录版本重建 `StateResult`，并在读取时复核 run hash、共同身份字段、连续 index 与 count；缺行或任何不一致都失败关闭。写入前拒绝一个 run 中混用的契约版本。空 `StateResult` 仍由 DTO 的 `min_length=1` 拒绝，不写空 run。

**已知可验证性边界：** `StateResult.result_hash` 不包含 `state_ref` 或 `spec_hash`，且表不重复保存完整请求。因此 writer 可以在写入时通过 `state_table` / `check_answers` 验证这两列与请求、规格一致；reader 可以验证它们在 run 的所有行中一致，但仅凭表行和 opaque `request_hash` 无法在读取时独立重建请求并重新证明二者的绑定。改变这一点需要契约或额外请求证据，不属于本 ADR；不另造 run identity，仍以 `result_hash` 为身份。

### 3. 分区

`day(evaluation_time)`。State 是密集时间序列，常见读取会按时间区间筛选；按日分区比月分区提供更细的区间裁剪。一个完整 StateResult 作为一次 append commit，因此同一 run 的数据一次写入并绑定同一 snapshot。未来若需调整分区，使用既有显式 partition-spec 演进与新定义版本。

### 4. 只追加 Iceberg 语义

- 一次 `StateResult` 是一个 batch，`batch_id = state.{result_hash}`。
- `StateTable.write(spec, request, result, descriptor)` 首先运行 `state_table` 的请求 / 规格 / descriptor 复核，再生成逻辑列与 run envelope。
- 已有 `result_hash` 下逐行完全相同则幂等重放、无新 snapshot；存在不同内容则 `StateTableConflict`，绝不追加或覆盖。
- 新 batch 基于固定 head snapshot 提交；`CommitConflict` 触发有界重新读取并重试；`BatchConflict` 失败关闭。提交后固定到新 snapshot 读回并逐行复核。
- `read(result_hash, expected_spec=None, snapshot_id=None)` 只在解析一次的 head 或调用方指定 snapshot 上读取；不会从缺失 snapshot 回退。提供 `expected_spec` 时，每行的 `state_ref` 与 `spec_hash` 必须分别等于其 `ref` 与内容哈希，否则以 `StateTableCorrupted` 失败关闭。此参数可选，以保持既有调用兼容；省略时仅验证 `StateResult` 哈希覆盖的载荷，无法认证 `state_ref` / `spec_hash` 两个目录列。`load` 同样接受并转发 `expected_spec`。
- 不提供更新、覆盖或删除。所有持久化 State 行只能通过显式 writer append。

### 5. 本地 artifact store

`StateResultStore(root)` 以 `<root>/<result_hash>.json` 存放完整 StateResult 的规范 JSON。写入使用同目录临时文件、fsync、原子 link 发布及目录 fsync；相同内容幂等，不同内容不覆盖。读取按记录版本重建并复核文件名、canonical bytes 与 `result_hash`；畸形、截断、重命名或篡改内容失败关闭。它是文件型运行 artifact，不代替 `state.states` 的 catalog snapshot。

### 6. 独立创建命令

新增 `python -m infrastructure.state.create_state_tables`：默认只显示安全计划/帮助，不加载 `Settings`、不打开 Catalog、不建目录或文件。只有显式 `--apply` 才打开配置中的 PostgreSQL Catalog 并 ensure `state.states`；适配器 registry 显式由 `PHASE1_TABLES + PHASE2_TABLES` 组合，但 ensure 范围仅限 `PHASE2_TABLES`。命令输出定义绑定与创建状态，不输出 DSN、凭据或异常文本。不得执行该命令连接真实 Catalog；本 ADR 仅授权实现与临时测试。

## 不改变的内容

- 不修改 `core/contracts/state.py`、其他契约或 `schemas/`。
- 不改变 `state_table` 的 9 列逻辑投影、StateRunner、StateProvider、StateSpec 或研究诊断语义。
- 不改变 Phase 1 表、注册表与 golden 哈希；不自动 provisioning；不改其他 Zone 表。
- 不连接真实 Catalog，不执行建表，不存储市场原始行情数据，不增加验证阈值。

## 测试范围

仅使用 `tmp_path` warehouse 和临时 SQLite PyIceberg Catalog：定义及字段 / partition ID、`day(evaluation_time)` 分区、Phase 1 registry 不变、ensure 幂等、写读往返与完整 run hash、幂等重写、冲突 / 篡改 / 缺行 / 重复 index / count 错误拒绝、固定 snapshot 读取、提供 `expected_spec` 时篡改或不匹配的 `state_ref` / `spec_hash` 必须拒绝、空 StateResult 契约拒绝、artifact store 原子写与 canonical read、默认命令零副作用、`--apply` 的显式 registry 和错误脱敏。测试不得使用真实 PostgreSQL 或 warehouse。
