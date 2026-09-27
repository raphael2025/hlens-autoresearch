# plugins/llm

`LLMProvider` 实现（[ADR-0040](../../docs/adr/0040-hypothesis-generation-and-llm.md)）。目前只有离线、确定性的
`ScriptedLLMProvider`（测试与演练；不联网、无凭据）。

> 实施说明：可选内容存储 CODE_COMPLETE / DEBUG_PENDING（无契约 / Schema 变化）。

## ScriptedLLMProvider

- 每次 `complete` 登记一个 `LlmCall`，prompt / input / output 都是 `ContentBlobRef`。
- `store=None`（默认）：引用为 `memory://sha256/<hash>`，**不可取回**；行为与哈希与引入 store 前逐字节相同
  （参考哈希固定在 `tests/plugins/llm/test_scripted_store.py`）。
- `store=<BlobSink>`：每个 blob 以 `canonical_json(payload)` 的 UTF-8 字节写入 store，`LlmCall` 记录 store 返回的
  可取回引用（`infrastructure.content.LocalContentStore` 给出 `cas://sha256/<hash>` + `byte_size`）；sha256 与无 store
  时相同，`LlmCall.content_hash` 因 uri / byte_size 不同而不同。store 返回的引用哈希或大小不符 → `RuntimeError`。
- `BlobSink` 是本包内的结构化 Protocol（`put(data, media_type) -> ContentBlobRef`）：plugins 不 import
  `infrastructure/`（`tests/test_feature_contracts.py` 边界测试）。

## 限制

- 研究循环 / 假设生成尚未传入 store，也未在登记时调用 `infrastructure.content.verify_llm_call`（跨 Phase 后续工作，
  由协调者安排）。
- 真实联网 Provider 需凭据与网络声明（`network=True`），不在本构建范围。
