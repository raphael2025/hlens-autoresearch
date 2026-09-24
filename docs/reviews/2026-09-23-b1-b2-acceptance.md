# B1 / B2 与 ADR-0010 纠偏的验收记录

| 项目 | 内容 |
|---|---|
| 文档性质 | 验收记录；不是状态文件、宪法、路线图或新的架构决定 |
| 批次日期 | 2026-09-23（B1、B2、ADR-0010 实施与裁决） |
| 最终复验 | 2026-09-24，Codex 对 `cd84a4e` 做最终独立复验并裁决 |
| 审查对象 | `4f83e18`（B1）、`4e0f6e3`（B2）、`cd84a4e`（ADR-0010 纠偏） |
| 分支 | `phase/0`，未合并 `main` |
| 裁决 | **B1、B2、ADR-0010 纠偏 ACCEPTED** |
| Phase | Phase 0 **仍未关闭** |
| 执行者 | Claude Code（Opus 5），实现与测试 |
| 复验者 | Codex（依 Raphael 2026-09-23 的技术决策授权），只读源码 / diff，并独立重跑既有检查 |

## 1. 审查范围

本次验收只覆盖三个 commit 的实际改动，不覆盖尚未实现的 Runner / Registry / 存储，也不覆盖
Constitution 批准与 Phase 0 关闭复审（批次 C）。

| Commit | 批次 | 对应 ADR | 交付要点 |
|---|---|---|---|
| `4f83e18` | B1 | [ADR-0008](../adr/0008-contract-payload-immutability.md) | 14 个映射字段改为只读 `FrozenMapping`（构造时复制并递归冻结）、逐模型内容哈希排除表、`canonical_json` 拒绝 NaN / Infinity 与静默兜底、`tests/vectors/v1/` 固定 v1 向量 |
| `4e0f6e3` | B2 | [ADR-0009](../adr/0009-experiment-identity-binding.md) | 完整实验身份（策略 / 风控 / Outcome 引用进入复现元组）、`dependency_hashes` 直接依赖内容绑定、`ExperimentRun` kind 校验与 `ValidationReport.run_id`、`ProfileSelection` 必填结构、`CONTRACT_SCHEMA_VERSION` 提升到 `2.0.0`、`core/compat/v1.py` 只读入口与 `schemas/v1/` 快照 |
| `cd84a4e` | ADR-0010 纠偏 | [ADR-0010](../adr/0010-contract-construction-and-canonical-versioning.md) | D-13 ~ D-16 的修复（见 §3、§4） |

验收方式：读取 diff 与源码、核对每个 ADR 的验收矩阵、独立重跑既有检查命令、用针对性探针
确认关键行为。复验者不新增也不修订测试代码；发现的问题返回 Opus 修复。

## 2. 初次复验发现的 P1 / P2

Codex 对 B1 / B2 做独立验收时，确认两份 ADR 的方向与验收矩阵已落地，但发现四处
"运行时承诺"与"实际执行"之间的缝隙。它们不改变已批准的架构方向，属于同一个**未发布**
2.0.0 在合并 / tag / 数据登记之前的纠偏。

| ID | 严重性 | 发现 |
|---|---|---|
| P1-a | P1 | `Contract.model_copy(update=...)` 是 Pydantic 的公开 API，但直接写入 `__dict__` 并绕过全部校验。因此 B1 的只读载荷与 B2 的身份绑定都能经**公开路径**被破坏：塞进可写 `dict`、与调用方共享别名、写入错误 `kind`、去掉 `dependency_hashes` 覆盖、把 `run_id` 置空。 |
| P1-b | P1 | 版本语法不唯一、不严格：`SEMVER_PATTERN` 用 `\d`，在 Python 中匹配 Unicode 数字（`١.٠.٠` 被接受）；允许前导零；不支持合法 build metadata；prerelease 的数字标识符前导零与空标识符未被拒绝。`schema_version` 的 major 另用 `int(value.split(".")[0])` 读取，与校验正则不同源。 |
| P1-c | P1 | `read_v1()` 只检查模型名与 major，会给任意字典铸造 legacy 身份：`{}`、`{"schema_version": "1.0.0"}`、错模型载荷、缺必填、多余字段、改了标签的 v2 载荷都能得到一个"看起来正经"的 v1 `content_hash`。 |
| P2-a | P2 | 导出的 JSON Schema 弱于运行时：`dependency_hashes` / `plugin_versions` / `StrategyArtifact.dependencies` 的键值格式只在 Python 侧强制，Schema 仅写 `additionalProperties: {"type": "string"}`，外部消费者无从得知规则，两套规则会各自漂移。 |

## 3. Codex 的裁决（D-13 ~ D-16）

四项由 Codex 依授权裁决，写入 [ADR-0010](../adr/0010-contract-construction-and-canonical-versioning.md)（Accepted），并交 Opus 实施：

| ID | 对应发现 | 裁决 |
|---|---|---|
| D-13 | P1-a | `model_copy(update=...)` 必须用 `{**__dict__, **update}` 重新构造并**完整校验**，返回同一具体模型类型；无 `update` 的普通 / 深复制保持 Pydantic 行为。`model_construct` 明确记录为面向可信数据的低层逃生口，**不是**受支持的外部载荷入口，不为其提供安全承诺。 |
| D-14 | P1-b | 全项目只有一套由组件常量拼成的 ASCII、完整 SemVer 2.0.0 语法；禁止 `\d`；禁止 core 与 prerelease 数字标识符的前导零；支持 build metadata；major 从**已验证的**命名分组读取。同 major 更高 minor 的含义精确为"版本号可识别，未知字段仍 fail closed"。 |
| D-15 | P1-c | `read_v1()` 在计算任何哈希**之前**，用已提交的 `schemas/v1/<Model>.schema.json` 做确定性顶层 shape gate：快照缺失即 fail closed；`required` 齐全；`additionalProperties: false` 时拒绝未知顶层字段；`schema_version` 必须是 v1 **已发布语法**的 `1.x.y[-prerelease]`，不套用 v2 新语法重写旧身份。 |
| D-16 | P2-a | 三类映射字段的键值格式标注在字段类型上（`RefKey` / `PluginKey` / `ContentHash`），Pydantic 用同一 pattern 字符串同时完成运行时校验与 Schema 导出，导出为 `patternProperties` + `additionalProperties: false`；不存在第二套来源。 |

裁决同时确认：因为 `2.0.0` 未合并 `main`、无 tag、无远程、无任何 v2 数据登记，这是对同一个
未发布版本的纠偏，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`；若将来已 tag 或已有数据登记，
同类收紧必须走新的 major。

## 4. `cd84a4e` 的修复与证据

- `core/domain/base.py`：`model_copy` 重新构造并完整校验；SemVer 组件常量、解析用命名分组、
  `RefKey` / `PluginKey` / `ContentHash` 标注类型。
- `core/compat/v1.py`：顶层 shape gate（含快照缺失 fail closed 与 v1 版本语法收紧）。
- `core/domain/research.py`、`core/domain/artifact.py`：三类映射字段改用标注类型。
- `schemas/`：36 份导出 Schema 同步键值 pattern；`schemas/v1/` 的 35 份快照**未参与本次导出**。
- 测试：新增 `tests/test_construction_and_versioning.py`（101 项），全套 545 项。
- red 证据（在 `4e0f6e3` 上实跑）：59 failed / 42 passed，覆盖 27 个测试函数——14 个映射字段的
  `model_copy` 别名与只读性、错误 `kind` / 缺依赖绑定 / 空 `run_id` / 非法版本 / 未声明字段的拒绝、
  合法与非法版本语法 12 / 17 例、plugin 与依赖键一致性、v1 gate 的 6 类反例与快照缺失
  fail closed、三类映射的 Schema 键值 pattern、文档边界。
- 文档：ADR-0010；`02-domain.md` §3.4 / §3.5；`06-experiment.md` §7 增加"绑定时
  `ValidationProfile` 必须 frozen"的 Runner / Registry 义务。未修改 ADR-0008 / 0009 正文，
  未修改 `docs/research/`（Constitution 与 roadmap）。

## 5. 最终复验的实测结果

Codex 在 `cd84a4e` 上实际重跑（原样摘要）：

| 检查 | 结果 |
|---|---|
| `pytest` | 545 passed |
| `ruff check` | All checks passed |
| `ruff format --check` | 94 files already formatted |
| `mypy` | Success: no issues found in 27 source files |

关键探针（Codex 独立执行）确认：

1. `model_copy(update=...)` 之后映射字段仍为 `FrozenMapping`，且与调用方传入的原始 dict 别名隔离；
2. 错误 `kind`、缺失依赖绑定、空 `run_id` 均被拒绝；
3. v1 只读入口接受合法 v1 载荷，拒绝错模型（wrong-model）载荷与多余顶层字段（extra）。

Schema 状态：current 36 份（`schema_version` 默认 `2.0.0`）、legacy 35 份（`schemas/v1/`）。
**v1 快照逐字节不变**，固定 v1 向量的旧哈希保持不变。

工作树在验收时为 clean。

## 6. 裁决

- **B1（`4f83e18`）、B2（`4e0f6e3`）、ADR-0010 纠偏（`cd84a4e`）ACCEPTED。**
- `cd84a4e` 作为当前 last known good。
- **Phase 0 仍未关闭。** 验收通过不等于 Phase 关闭：批次 C（关闭复审）未执行，
  Constitution 仍为 0.2.0-draft 且须由 Raphael 亲自批准，契约 2.0.0 只在 `phase/0` 生成，
  尚未合并 `main`、无 tag、无远程发布、无任何 v2 数据登记。

## 7. 边界（必须如实表述）

- 契约层只校验**直接引用**的内容绑定。传递依赖闭包、`params` 默认值展开、
  `run.repro` ↔ Spec 一致性、trial 权威账本、Registry 存在性与 `ValidationProfile` frozen
  校验，都是**未实现的** Runner / Registry 义务（`06-experiment.md` §7）。
- `core/compat/v1.py` 的 gate 只做**顶层**形状检查，不是完整 JSON Schema 递归校验：
  不校验嵌套结构、类型、取值范围与数组元素，也未引入 `jsonschema` 依赖。
- `model_construct()` 不在安全承诺范围内；受支持的入口是构造函数、
  `model_validate` / `model_validate_json` 与 ADR-0010 定义的 `model_copy`。
- 读取 v1 记录**不赋予**任何 v2 登记或晋升资格；v1 与 v2 的
  `content_hash` / `experiment_hash` 不可比较。
- 外部是否存在 v1 历史数据，证据不足：**不宣称**迁移路径已在真实数据上验证。
- 本次验收未运行任何实验，仓库内没有持久化的实验 / Run / Profile / 报告。

## 8. 剩余项与下一步授权边界

Codex 本轮只授权 **B3 的只读规划与 ADR 起草**；**尚未授权 B3 的代码实现**。
下列为 B3 的**候选范围**，是待规划的问题清单，**不是已决定的方案**；具体取舍以后续获批的
ADR 为准，不得在规划阶段写成结论或提前改动契约。

| 候选 | 问题 |
|---|---|
| 生命周期审批 / 主体 / 授权期 | `REVALIDATION → RETIRED` 等转移的审批要求校验；`LifecycleHistory` 构造是否校验 `transition.subject` 一致性；执行授权的有效期语义（过期授权当前可被接受）|
| Outcome 输入与 lag | Feature / State 可接受 Outcome 引用；Event lag 可为负；是否在契约层收紧 |
| 防"证据不足即 PASS" | 含 INCONCLUSIVE 的门与仅 G0 的门当前可整体 PASS；需要区分 DTO 不变量与未来验证门服务 |
| 审计哈希字段统一 | 审计 / 记录类字段与内容哈希边界的统一表述 |
| `LlmCall` 完整 I/O | `06-experiment.md` §2 要求完整输入输出，当前只存三个哈希，是未关闭缺口 |
| Runner / Registry 义务边界 | 传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性、Registry 存在性的责任划分与交付时点 |
| Provider 验收范围漂移 | `05-plugin.md` 承诺的 Phase 0 Provider 签名与 roadmap 验收表不一致，需要明确实施或正式延期 |

其他仍然开放、不在本轮范围：Constitution 批准为 1.0.0、Phase 0 关闭、合并 `main`、创建 tag、
远程仓库位置、D-09 数值 TBD-1 ~ TBD-5、H-3 ~ H-7、Q-1 ~ Q-7。
