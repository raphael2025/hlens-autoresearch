# tests/

测试。计划分层：contract（契约与 Schema）、architecture（依赖方向 / 导入检查）、unit、integration、reproducibility（重跑一致性）、validation-negative-controls（已知过拟合样例必须被拒绝）。不得为通过测试而削弱测试（CLAUDE.md H4）。

现有测试文件（与 `tests/test_*.py` 一一对应）：

| 文件 | 覆盖 |
|---|---|
| `test_contracts.py` | 契约通用规则、Schema 导出、point-in-time 与 Outcome 规则 |
| `test_architecture_boundaries.py` | 依赖方向与导入检查（契约层无基础设施依赖） |
| `test_docs_consistency.py` | 文档一致性（Constitution 无数值、ADR 索引、链接） |
| `test_payload_immutability.py` | ADR-0008 只读载荷与内容哈希载荷边界 |
| `test_experiment_identity.py` | ADR-0009 实验身份 + 契约 2.0.0 发布验收、v1 只读边界 |
| `test_construction_and_versioning.py` | ADR-0010 构造路径 / 版本语法 / v1 shape gate / Schema 格式 |
| `test_lifecycle.py` | ADR-0006 + ADR-0011 状态机、主体一致性、授权与时间顺序 |
| `test_information_flow.py` | ADR-0012 信息流白名单与 `kind` 判别字段 |
| `test_deterministic_validation.py` | ADR-0013 确定性判定函数与数值合法性 |
| `test_validation_profile_invariants.py` | ADR-0014 Validation Profile 的普适结构不变量 |
| `test_audit_identity.py` | ADR-0015 审计身份类型（`ContentHash` / `GitOid` / `GitCodeRevision`）与版本绑定 |
| `test_validation_architecture.py` | ADR-0007 三层验证架构 |

`factories.py` 是共享的最小合法对象构造器；其中的数字与哈希都是**测试数据**，不是被批准的阈值。

`vectors/v1/` 是 **v1 legacy 语义**的固定载荷与旧哈希快照，用 commit `066b22d` 的代码生成，
时间固定、不依赖 `now`；只读，不得用新实现重新生成（CLAUDE.md H6）。

> reproducibility（重跑一致性）与 validation-negative-controls（已知过拟合样例必须被拒绝）
> 需等待对应 Phase 开启（见 docs/research/roadmap.md）。
