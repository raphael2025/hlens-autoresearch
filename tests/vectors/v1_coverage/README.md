# v1 只读覆盖向量（ADR-0016 实现批次新增）

本目录**不是** `tests/vectors/v1/` 的一部分，也**不属于**那份用 commit `066b22d` 的代码
生成的冻结快照集。这里的文件是 **ADR-0016 实现批次（2026-09-24）新增的兼容测试资产**，
用来补上 [ADR-0016](../../../docs/adr/0016-llmcall-content-bindings.md) 验收矩阵第 15 项
（v1 的三哈希 `LlmCall` 载荷经 `read_v1` 读取、旧哈希不变）——`tests/vectors/v1/` 里没有
独立的 `LlmCall` 向量，而那些文件只读、不得改写或重新生成（CLAUDE.md H6）。
单独建目录正是为了不去动它们。

| 项 | 说明 |
|---|---|
| 来源 | 新构造的 **v1 形状** `LlmCall` 载荷，字段与 `schemas/v1/LlmCall.schema.json` 一致 |
| 期望值 | 由 **v1 读取语义**生成后固定：`sha256(canonical_json(payload - legacy_excluded_fields))`，与 `core/compat/v1.py` 的 `read_v1` 同一规则 |
| 不是什么 | **不是**历史上真实存在过的 v1 记录；**不是**用 current `LlmCall` 重写的旧语义 |

**这个向量证明什么**：v1 只读路径在 ADR-0016 之后仍然接受旧的三哈希 `LlmCall` 形状，
且其 v1 内容哈希按 v1 规则计算并保持稳定——current `LlmCall` 换成内容引用不会倒灌进 v1。

**不证明什么**：外部是否存在 v1 历史数据（证据不足，见 `PROJECT_STATUS.md` §7）；
读取结果仍只是 `LegacyV1Record`，不获得任何 v2 登记 / 晋升资格（ADR-0009 §7）。

文件格式与 `tests/vectors/v1/README.md` 相同（`contract_schema_version` / `model` /
`legacy_excluded_fields` / `legacy_content_hash` / `payload`），另加 `generated_by`
标明来源。生成后只读：不得用新实现重新生成或"修正"。
