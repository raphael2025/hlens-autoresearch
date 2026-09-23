# tests/

测试。计划分层：contract（契约与 Schema）、architecture（依赖方向 / 导入检查）、unit、integration、reproducibility（重跑一致性）、validation-negative-controls（已知过拟合样例必须被拒绝）。不得为通过测试而削弱测试（CLAUDE.md H4）。

现有测试文件：`test_contracts.py`、`test_payload_immutability.py`（ADR-0008）、
`test_experiment_identity.py`（ADR-0009 + 契约 2.0.0 发布验收）、
`test_construction_and_versioning.py`（ADR-0010 构造路径 / 版本语法 / v1 gate / Schema 格式）、
`test_lifecycle.py`、
`test_validation_architecture.py`、`test_architecture_boundaries.py`、`test_docs_consistency.py`。

`vectors/v1/` 是 **v1 legacy 语义**的固定载荷与旧哈希快照，用 commit `066b22d` 的代码生成，
时间固定、不依赖 `now`；只读，不得用新实现重新生成（CLAUDE.md H6）。

> reproducibility（重跑一致性）与 validation-negative-controls（已知过拟合样例必须被拒绝）
> 需等待对应 Phase 开启（见 docs/research/roadmap.md）。
