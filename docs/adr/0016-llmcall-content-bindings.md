# ADR-0016: `LlmCall` 的最小完整登记

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24 起草，等待 Codex 文档复核；未获批准，不得实施） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3）；首次被消费在 Phase 7 |
| 影响范围 | Contract / 审计 / 安全 |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0009](0009-experiment-identity-binding.md) §5、[ADR-0015](0015-audit-identity-types-and-version-bindings.md) |

## 背景

`06-experiment.md` §2 要求复现元组的 `llm_calls` 记录"provider、model、prompt 哈希、
**完整输入输出**"，`05-plugin.md` §1 要求"非确定性插件必须记录全部输入输出"，
`09-security.md` §3 要求"记录全部 LLM 调用以供审计与复现"。

当前 `LlmCall`（`core/domain/research.py:98-105`）只有五个自由字符串：
`provider`、`model`、`prompt_hash`、`input_hash`、`output_hash`。三个哈希没有格式约束，
也没有任何取回路径——**只有哈希，没有内容**。ADR-0009 §5 已把这一缺口记为待关闭的义务，
并说明"完整内容或可取回引用的实现要等存储层就位后才能完成"。`06-experiment.md:47-50`
同样写明：不得把"仅存哈希"描述为已满足该要求。

存储层仍未就位（D-01、D-02 未决），但**契约可以现在就规定"登记什么"**：
一个内容引用需要哪些槽位，与那些槽位由哪种存储填充是两件事。

## 精确决定

### 1. 新增值对象 `ContentBlobRef`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `uri` | 非空字符串 | 是 | 内容的取回位置；方案与布局不在本 ADR 定义 |
| `sha256` | `ContentHash` | 是 | 内容的 SHA-256（64 位小写十六进制，ADR-0015 §D-21.1 的同一类型） |
| `media_type` | 非空字符串 | 否 | 提供时不得为空串 |
| `byte_size` | 非负整数 | 否 | 提供时 `>= 0`（允许零字节内容） |

`uri` 只做"非空"约束：URI 方案取决于尚未决定的存储选择（D-01、D-02），
现在收紧会把一个未做的决定写死进冻结契约。

### 2. `LlmCall` 的最小完整登记

| 字段 | 决定 |
|---|---|
| `prompt` | `ContentBlobRef`，**必填** |
| `input` | `ContentBlobRef`，**必填** |
| `output` | `ContentBlobRef`，**必填** |
| `provider` | 非空字符串（现状为无约束字符串，收紧为 `min_length=1`） |
| `model` | 非空字符串（同上） |
| `called_at` | `UtcDatetime`，必填并带默认值（`datetime.now(UTC)`，与其它审计时间字段一致） |

原有的 `prompt_hash` / `input_hash` / `output_hash` 三个字符串字段被上述三个值对象**取代**：
哈希继续存在，位置在 `ContentBlobRef.sha256`，并且第一次带上了取回路径与格式约束。

三项**全部必填**：一次 LLM 调用总是有提示、有输入、有输出。允许其中任何一项缺失，
等于允许记录一次无法复核的调用。

### 3. 可取回性与内容一致性延期

**严禁自报 `verified` 布尔。** 契约层无法打开 `uri`，因此：

- `uri` 是否可取回；
- 取回的内容是否真的哈希成 `sha256`；
- `media_type` / `byte_size` 是否与实际内容相符；

这三件事全部由**存储层 / Registry** 在持有内容时核验，本 ADR 不实现、不声称已实现，
也不引入任何"我已验证"的布尔标志（与 ADR-0009 §6 的既有立场一致）。

**诚实边界（必须如实表述）**：本 ADR 把 `LlmCall` 从"只有哈希"提升为
"哈希 + 取回引用 + 调用时间"，这是**登记结构**的完整化。
在存储层就位并能取回内容之前，**不得**把 `06-experiment.md` §2 的
"完整输入输出"要求描述为已经满足。该文档中的缺口说明在存储层落地前继续有效。

## 明确不做

- **不实现 LLM**：不引入任何 LLM SDK、不调用任何模型、不定义 prompt 模板或输出 schema。
- **不实现存储或网络**：不定义 URI 方案、不写入对象存储、不做任何取回。
- 不引入 `verified` 或任何自报验证标志。
- 不定义 provider / model 的取值集合或命名规范。
- 不定义 token 计数、费用、延迟、温度等调用参数字段——它们属于首次消费 LLM 的 Phase，
  由那时的实际需要决定（ADR-0017 的交付节奏）。
- 不定义 `LLMProvider` 的可执行 Protocol（见 [ADR-0017](0017-provider-delivery-schedule.md)）。
- 不改变 `09-security.md` 的 LLM 安全规则（LLM 只产出数据，永不裁决）。

## 运行时延期义务

| 义务 | 说明 |
|---|---|
| URI 可取回性 | 内容是否存在、是否可读、权限是否正确，属存储层 |
| 内容 - 哈希一致 | 取回内容的 SHA-256 是否等于 `sha256`，属存储层 / Registry |
| 不可变性 | 已登记的内容不得被覆盖或删除（追加式存储的义务） |
| 数据外发合规 | 哪些数据可以发给外部 LLM（`09-security.md` §3），属应用层策略 |
| 完整性覆盖 | 一次实验是否登记了**所有**发生过的 LLM 调用，契约层无法判断，属 Runner |

## Schema 与迁移影响

- 受影响模型：`LlmCall`（被 `ReproducibilityTuple.llm_calls` 与
  `ExperimentMetadata.llm_calls` 使用）。
- **新增契约模型 `ContentBlobRef`**：必须登记进 `CONTRACT_MODELS` 并导出 Schema，
  计入"所有核心实体都有契约与 Schema 导出"这条验收标准。
- **迁移**：v2 **尚未发布**，因此迁移只保留 **v1 reader**——`core/compat/v1.py` 的
  `read_v1` 继续按 v1 语义读取旧的三哈希 `LlmCall` 载荷，其 `schemas/v1/LlmCall.schema.json`
  快照与固定向量逐字节不变。**新模型不进入 v1 清单**：`ContentBlobRef` 不加入
  `V1_MODEL_NAMES`，`schemas/v1/` 不新增任何文件。
  v1 载荷读取后仍然只是 `LegacyV1Record`，不因此获得 v2 登记 / 晋升资格（ADR-0009 §7）。
- 需要重新导出 `schemas/`；顶层 Schema 数量增加，实际数量与差异以实施时的重导出结果为准。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
也没有任何 v2 数据登记（仓库内没有任何 LLM 调用记录）。因此即使本 ADR 替换了
`LlmCall` 的三个字段，它仍是对同一个未发布版本的收窄，
`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本矩阵是**未来实现批次**的验收条件，本轮只起草，未运行、未实现。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | `LlmCall` 缺 `prompt` / `input` / `output` 任一项 | 逐项拒绝 |
| 2 | 三项齐全且合法 | 接受 |
| 3 | 载荷中仍使用旧的 `prompt_hash` / `input_hash` / `output_hash` | 拒绝（`extra="forbid"`） |
| 4 | `ContentBlobRef.uri` 为空串或纯空白 | 拒绝 |
| 5 | `ContentBlobRef.sha256` 非 64 位小写十六进制（含大写、长度错、非十六进制） | 拒绝 |
| 6 | `media_type` 提供为空串 | 拒绝；不提供时接受 |
| 7 | `byte_size` 为负 | 拒绝；为 `0` 时接受；不提供时接受 |
| 8 | `provider` / `model` 为空串 | 拒绝 |
| 9 | `called_at` 为 naive datetime | 拒绝（`UtcDatetime`） |
| 10 | 不传 `called_at` | 接受，取默认值 |
| 11 | `LlmCall` 出现任何自报验证布尔字段 | 不存在 |
| 12 | `ContentBlobRef` 已登记进 `CONTRACT_MODELS` 并导出 Schema | 是 |
| 13 | `V1_MODEL_NAMES` 是否包含 `ContentBlobRef`；`schemas/v1/` 文件数 | 否；仍为 35 份 |
| 14 | v1 的三哈希 `LlmCall` 固定向量经 `read_v1` 读取 | 接受，旧哈希不变 |
| 15 | 含 `llm_calls` 的复现元组 JSON 往返 | `experiment_hash` 按位一致 |
| 16 | `06-experiment.md` 的缺口说明 | 在存储层就位前保持存在，不得删改为"已满足" |

## 后果

- 正面：一次 LLM 调用第一次有了"内容在哪里 + 内容是什么"的完整槽位；三个哈希获得格式约束；
  调用时间进入审计记录；`09-security.md` §3 的审计要求有了可填充的结构。
- 负面 / 代价：登记一次 LLM 调用的成本上升——调用方必须先把三份内容落到某个可取回位置
  才能构造记录；在存储层就位前，这会限制 `LlmCall` 的实际可用性。
  这是刻意的：宁可暂时不可用，也不要一条无法复核的审计记录。
- 对复现性的影响：v1 只读路径与旧哈希逐字节不变；仓库内无 LLM 调用记录受影响。

## 合规检查

- [ ] 不实现 LLM、存储或网络；Domain 层仍只依赖标准库与 Pydantic
- [ ] 不引入自报验证布尔标志
- [ ] 未把"仅存哈希"或本 ADR 的结构化登记描述为已满足"完整输入输出"
- [ ] 不修改 Validation Constitution、roadmap 与任何已批准 ADR 的正文
- [ ] 未合并 `main`、未创建 tag
