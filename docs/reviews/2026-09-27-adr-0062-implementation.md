# ADR-0062 实施说明：Validation Profile 冻结登记（B56）

| 字段 | 值 |
|---|---|
| 性质 | 实施记录，**不是验收结论**；ADR-0062 仍为 **Proposed**，等待 Codex 对代码、测试与门禁的最终复核 |
| 决定来源 | Codex 依 Raphael 全权授权（2026-09-27）：`ValidationProfile.status` 不进内容哈希（ADR-0008），Promotion 不能把调用方给的 `status = FROZEN` 当作权威；设计问题 Q1 = A（独立登记）、Q2 = A（校准证据只验真实性与引用一致），复核后追加"目录外锚点必需" |
| 实施者 | Claude Code（Opus），只实现，不改变决定 |
| 分支 | 本地 `claude/adr-0055-integration`，逐批普通快进推送到 `origin/wip/all-code-completion` |
| 状态 | CODE_COMPLETE / DEBUG_PENDING |

## 1. 提交

| 提交 | 内容 | 复核 |
|---|---|---|
| `3e1dca8` | ADR-0062（Proposed）+ ADR 索引，docs-only | Codex 通过（锚点改为必需后） |
| `6e482e2` | 阶段 1：`infrastructure/registry/profile_freeze.py`（`ProfileFreezeRegistry`）与测试 | Codex 发现两处阻断（见 §3） |
| `a89b00a` | 阶段 1 返修：追加路径失败即作废实例；写任何 blob 之前先跑全部规则 | Codex 复核通过 |
| `7f93629` | 阶段 2：`research/promotion` 以冻结登记为权威冻结来源 | Codex 复核通过 |
| 本提交 | 阶段 3：文档同步（本说明、完成计划、STATUS、MEMORY、ADR 索引与说明） | 待复核 |

## 2. 实现了什么

- **`ProfileFreezeRegistry(root, *, anchor)`**（`infrastructure/registry/profile_freeze.py`）：直接组合既有 `AppendOnlyJournal`（`freezes.jsonl`）、
  `BlobStore`（`blobs/`）、`fcntl.flock`（`.lock`）与**必需**的目录外锚点日志；未改 `StrategyRegistry`；只依赖 `core` 与 `infrastructure`。
- **唯一记录 `profile.frozen`**，键恰为 `format_version`（1.0.0）、`profile`（完整 JSON）、`profile_ref`、`profile_hash`、`calibration`
  （`kind` / `report_hash` / `sha256` / `uri`）、`approved_by`、`approved_at`（UTC，`Z`）、`freeze_id`。
- **登记**：Profile 重新校验且 FROZEN；校准报告须为原始规范 JSON 字节（按原样固化为 blob，不重新序列化），`kind = gate_calibration`，
  自哈希成立；`provenance.calibration_report` 逐字等于 `report_hash`；具名批准人；带时区时间，存为 UTC。**全部规则**（含状态、
  引用、重复 / 冲突、报告）在写任何东西之前对给定字节执行——被拒的登记不改变日志、锚点与 `blobs/`。
- **重放**：与追加同一套规则，blob 每次打开都重新验证；任何违例 → `RegistryCorrupted`（登记无法打开）。重复 → `DuplicateRecord`，
  同 ref 另一哈希 → `FreezeConflict`。
- **锚点**：缺失或位于目录内即拒绝打开；整行截断、历史被改写、锚点之外多写 → 损坏；唯一崩溃窗口（记录已写、锚点未写）打开时补写。
- **写路径失败**：一旦开始写（blob、日志、锚点）任何异常即**作废并关闭**实例：`frozen_record`、`freeze_of`、`freezes`、`len` 与后续写入
  全部 `RegistryCorrupted`，直到新实例重放并校验磁盘；冻结记录只在记录与锚点都落盘后才进入内存。
- **Promotion**（`research/promotion/service.py`）：`build_artifact(evidence, *, freezes)` / `promote(evidence, registry, *, freezes)`；
  每份报告的 Profile 在既有检查（FROZEN、已校准、哈希）之后，还必须有恰好对应其 ref 与内容哈希、引用其校准报告的登记记录；
  没有、只有同 ref 另一哈希、或登记已关闭 / 作废 / 缺失 / 类型不对 → `profile_not_frozen`；`created_at` 不得早于冻结批准时间。

## 3. 复核与返修（阶段 1）

Codex 复核 `6e482e2` 的两处阻断，均在 `a89b00a` 修复（不改写历史）：

1. 日志已追加、内存已接纳后锚点写入失败，实例仍可返回新冻结；日志写入中途 `OSError` 可能使日志与内存分歧 → 改为写路径任何异常即作废并关闭，
   内存只在记录与锚点都落盘后接纳。故障注入测试：锚点在日志 fsync 之后失败（作废；重开补写锚点）、日志写入部分字节后失败（作废；重开因
   部分尾行 fail closed）、日志整行写入后 fsync 失败（作废；重开按崩溃窗口补写）、blob 写入失败（作废；重开无记录）。
2. DRAFT Profile 在 `_apply` 拒绝前已写入 blob → 先构造完整载荷（blob 哈希由给定字节算出）并对内存中的报告执行全部规则，再写。测试：DRAFT /
   重复 / 冲突 / 未引用报告的登记使全部文件逐字节不变；空登记上的 DRAFT 不产生 blob。

"`_open_anchor` 中重复的不可达 `raise`"：在 `6e482e2` 中未找到；最接近的是 `_apply` 中两个相同的 `except BlobMissing` / `except BlobCorrupted`
分支，已合并为一个。`__init__` 中 `anchor is None` 的运行时守卫保留（有测试）。

## 4. 测试与验证（原样结果）

- 阶段 1 返修后：`pytest tests/promotion tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → `172 passed`；
  `tests/promotion/test_profile_freeze_registry.py` → `56 passed`；ruff / format（13 files）/ mypy（12 files）通过。
  6 项阻断测试在 `6e482e2` 的模块上失败、在 `a89b00a` 上通过。
- 变异（逐一施加，测试均失败）：去掉 blob 重放验证、锚点截断检查、冲突检查、`format_version` 检查、重放 provenance 检查、UTC 检查、作废逻辑、
  "blob 先于规则"。唯一存活：把内存接纳移到锚点写入之前——作废已阻止任何读取，因此不可观察（纵深防御，未为私有状态写测试）。
- 阶段 2 提交前（该树即 `7f93629`）：`pytest -m "not postgres" tests/promotion tests/research tests/apps tests/test_*.py` →
  `4615 passed, 1 warning in 1293.77s`，退出码 0；`ruff check .` → `All checks passed!`，退出码 0；`ruff format --check .` →
  `760 files already formatted`，退出码 0；`mypy` → `Success: no issues found in 595 source files`，退出码 0。
- 阶段 2 变异：去掉登记检查、缺记录时信任 status、不把关闭 / 作废当作无答案、去掉类型检查、时间检查不含冻结时间——均使测试失败。
- ADR commit `3e1dca8` 推送前：`pytest tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → `20 passed`；`git diff --check` 无输出。

## 5. 未完成边界（诚实边界）

- `approved_by` 只是被声明的名字：不认证操作系统用户或任何身份，不证明批准人权限。
- 本地文件登记**不是**生产 Control Plane，不授权实盘、资金或生产部署；ADR-0005 的生产部署审查与 D-01 / D-02 的 PostgreSQL Control Plane 仍在范围外。
- 锚点与登记被一起回滚仍不可检测；本批只要求锚点在登记目录之外。
- 登记是空的：今天没有任何冻结记录，所有 Promotion 仍以 `profile_not_frozen` 被拒；真实冻结记录只能按 Raphael 的冻结决定（D-09 TBD-1..5）写入；
  本批不选择、不冻结任何 Profile 数值。
- 没有解冻 / supersede / 撤销记录类型；Profile 从 FROZEN 到 SUPERSEDED 的登记需要另立决定。
- 校准报告是否适用于该 Profile 不由登记判断（ADR-0062 Q2 = A），由具名批准承担。
- 未改 Domain Contract、Schema、Profile 结构 / 数值 / 哈希、Constitution、ADR-0008；ADR-0062 仍为 Proposed。
