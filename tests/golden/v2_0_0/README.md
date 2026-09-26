# v2.0.0 固定载荷与哈希（ADR-0052 §4 前置）

本目录是**契约 Schema 2.0.0（`CONTRACT_SCHEMA_VERSION = 2.0.0`）现有载荷的固定快照**，
在任何 ADR-0052 契约改动之前（commit `86bdcb6`）生成，用来证明：以后无论契约如何演进
（ADR-0052 的可选字段、2.1.0 minor），这些 2.0.0 记录仍按**记录时的版本**读取、校验，且内容哈希逐位不变。

| 文件 | 模型 | 说明 |
|---|---|---|
| `gate_result.json` / `gate_result_no_threshold.json` | `GateResult` | 带阈值 / 纯报告项 |
| `validation_report.json` | `ValidationReport` | 三个门（含一个 17 位有效数字的 p 值） |
| `validation_profile.json` / `validation_profile_frozen.json` | `ValidationProfile` | 带 `inconclusive_bands` 与 `degradation_thresholds` / frozen 版 |
| `revision_record.json` | `RevisionRecord` | 一条 Canonical 行背后的契约对象；其信封版本即 Canonical 表的 `contract_schema_version` 列 |

键：`contract_schema_version`（载荷的记录版本）、`model`、`generated_at_commit`、`content_hash`
（生成时 `obj.content_hash()`）、`payload`（完整 `model_dump(mode="json")`）。

生成方式：一次性脚本，不随仓库提交；时间输入全部固定。**不得重新生成或"修正"这些文件**（H6）；
测试：`tests/test_v2_golden_vectors.py`。不放在 `tests/vectors/` 下：那里是 v1 冻结目录，
`tests/test_adapter_contracts.py` 按文件集合逐字节锁定。
