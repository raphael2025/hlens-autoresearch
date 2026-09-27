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

- 内容可取回与一致性核对只在研究循环**选用**时生效：`research/loop/llm_content.py` 的 `ContentVerifiedLLM(inner, resolver)`
  在应答后经 resolver（例如 `infrastructure.content.LocalContentStore`，`verify_llm_call` 重算每个 blob 的哈希与大小）
  取回三个引用并与实际的提示、输入、输出比对，不符即 `LlmContentUnverified`；假设阶段取用已审阅草稿时再核对一次。
  不包装时行为与记录哈希不变。
- 真实联网 Provider 需凭据与网络声明（`network=True`），不在本构建范围。
