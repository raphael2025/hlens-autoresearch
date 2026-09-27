# infrastructure/content

本地、只追加、内容寻址的 blob 存储（Phase 7）：为 `LlmCall` 的 prompt / input / output 提供可取回、可复核的存储层
（[ADR-0016](../../docs/adr/0016-llmcall-content-bindings.md) §D-18.3 的存储层义务；[ADR-0040](../../docs/adr/0040-hypothesis-generation-and-llm.md)）。

> 实施说明：CODE_COMPLETE / DEBUG_PENDING（无契约 / Schema 变化）。

## 组成

| 名称 | 内容 |
|---|---|
| `LocalContentStore(root)` | `<root>/sha256/<前两位>/<sha256>`；`put(bytes, media_type)` → `ContentBlobRef(uri="cas://sha256/<hash>", byte_size=...)`；`put_json(payload)` 存 `canonical_json(payload)` 的 UTF-8 字节，SHA-256 与 `content_hash(payload)` 相同 |
| `get(ref)` | 每次读取都重新哈希并核对 `byte_size`；缺失 → `BlobMissing`，篡改 / 截断 / 大小不符 → `BlobCorrupted`，非本存储的 URI → `ContentStoreError` |
| `verify_llm_call(call, resolver)` | 取回一次调用的三个 blob，要求规范 JSON，返回 `LlmCallContent` |

写入：临时文件 → `fsync` → `os.link`（不覆盖已有文件）→ 目录 `fsync`；相同字节重复写入幂等；地址上已有不同字节
（被篡改）→ `BlobConflict`，绝不覆盖。无删除、无覆盖；发布后的 blob 设为只读。

## 限制

- `cas://sha256/<hash>` 只是本存储的本地 URI 方案；契约只要求 `uri` 非空（D-01 / D-02 未决，未冻结方案）。
- 证明"登记的调用可取回且未被改动"，**不**证明一次实验登记了它发生过的**全部**调用（Registry / Runner 义务）。
- 研究循环支持显式选择 `ContentVerifiedLLM(inner, resolver)`，对 prompt / input / output 做可取回性与内容核验；该模式有定向测试，但默认调用方不自动配置持久 store。未使用包装器时，`ScriptedLLMProvider` 的 `memory://` 引用不可取回。
- 单机 POSIX；没有垃圾回收、没有远程后端、没有配额。
