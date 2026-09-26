# infrastructure/migration

技术迁移工具（Phase 14，[ADR-0047](../../docs/adr/0047-migration-framework.md)；
[10-migration.md](../../docs/architecture/10-migration.md)）。

> 实施说明：金标准持久化、重跑报告、回滚证据与事件总线一致性桥接 CODE_COMPLETE / DEBUG_PENDING（无契约 / Schema 变化）。

## 组成

| 模块 | 内容 |
|---|---|
| `golden.py` | `record_golden` / `compare_golden`（容差由迁移 ADR 声明，0 = 按位一致）；`save_golden` 以 `<record_hash>.json` 内容寻址写入（只写一次、不覆盖），`load_golden` 重新哈希并核验 `outputs_hash` 与规范形式；`GoldenDiff.report()` 确定性的纯文本重跑报告（含 golden / rerun 哈希） |
| `rollback.py` | `rollback_evidence(migration_id, golden, migrated, rolled_back)` → `RollbackEvidence`：回滚后的重跑（必须以容差 0 比较）与金标准按位一致才是 `RESTORED`；**只是证据**，不执行回滚、不作迁移裁决 |
| `conformance.py` | `run_conformance`：对每个检查新建候选实现，逐条报告失败 |

`tests/infrastructure/migration/` 通过 `run_conformance` 运行 Knowledge 契约套件，以及（测试侧桥接）
`tests/contract_suites/event_bus.py` 在 `InMemoryEventBus` 与 `FileEventBus` 上的套件。

## 限制

- 没有选定具体迁移目标，也没有登记金标准实验集合（需 P4 / P8 实验产出后由迁移 ADR 选定）。
- 金标准输出只支持具名有限 `Decimal`；不覆盖文件型产物（数据集、模型）的比对。
- 回滚证据不绑定具体部署或数据版本；由迁移 ADR 引用。
