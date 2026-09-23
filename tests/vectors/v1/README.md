# v1 固定测试向量

本目录是**契约 Schema v1（`CONTRACT_SCHEMA_VERSION = 1.0.0`）的固定载荷与旧哈希快照**，
用于 [ADR-0008](../../../docs/adr/0008-contract-payload-immutability.md) §6 与
[ADR-0009](../../../docs/adr/0009-experiment-identity-binding.md) §7 要求的 v1 只读兼容验收。

| 项 | 说明 |
|---|---|
| 生成代码 | commit `066b22d` 的 `core/`（v1 语义，尚未实施 ADR-0008 / 0009） |
| 生成方式 | 一次性脚本，**不随仓库提交**；这些文件此后只读 |
| 时间输入 | 全部固定为 `2024-01-01T00:00:00Z`，不依赖 `datetime.now()` |

## 文件格式

| 键 | 含义 |
|---|---|
| `contract_schema_version` | 载荷遵循的契约版本（恒为 `1.0.0`） |
| `model` | v1 模型名 |
| `legacy_excluded_fields` | **v1** 的非语义字段排除表（v1 `Contract._non_semantic_fields()` 的实际取值） |
| `legacy_content_hash` | v1 语义下的内容哈希：`sha256(canonical_json(payload - legacy_excluded_fields))` |
| `payload` | v1 模型的完整 `model_dump(mode="json")` |

## 这些向量验证什么

- 固化 **v1 legacy 语义**：`canonical_json` 的键排序 / 分隔符 / `ensure_ascii=False`，以及 v1 的
  逐字段排除规则。它们**不是** v2 的期望值。
- `validation_profile_draft` 与 `validation_profile_frozen` 内容完全相同、仅 `status` 不同，
  其 v1 哈希**不相等**——这正是 ADR-0008 §3 要修正的 v1 缺陷，保留在此作为历史证据。
- `experiment_spec` 的 `strategy` / `risk_policy` / `outcome` 与 `repro` 并列存放，
  是 ADR-0009 §1 要消除的 v1 结构。

不得用新实现重新生成或"修正"这些文件（H6）。
