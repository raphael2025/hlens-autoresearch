# ADR-0010: 契约构造路径、规范版本语法、v1 顶层 shape gate 与 Schema 格式表达

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-23；Raphael 授权 Codex 作项目技术裁决） |
| 日期 | 2026-09-23 |
| 决策者 | Codex（依据 Raphael 2026-09-23 的项目技术决策授权） |
| 起草者 | Codex 裁决；Claude Code（Opus）实现与文档落地 |
| 相关 Phase | Phase 0 |
| 影响范围 | Contract |
| 是否破坏兼容 | **否**（收紧既有约束；`CONTRACT_SCHEMA_VERSION` 仍为 `2.0.0`） |
| 前置 | [ADR-0008](0008-contract-payload-immutability.md)、[ADR-0009](0009-experiment-identity-binding.md)（均 Accepted，B1/B2 已实施） |

## 背景

Codex 对 B1（`4f83e18`）与 B2（`4e0f6e3`）做独立验收后，发现四处"运行时承诺"与"实际执行"
之间仍有缝隙。它们都不改变已批准的架构方向，属于 v2 **合并 / tag / 数据登记之前**的纠偏。

1. **D-13 复制更新绕过校验。** `Contract.model_copy(update=...)` 是 Pydantic 的公开 API，
   但它直接写入 `__dict__`，绕过全部校验。因此 ADR-0008 的只读载荷与 ADR-0009 的身份绑定
   都能被公开路径破坏：可以塞进可写 `dict`、与调用方共享别名、写入错误 `kind`、
   去掉 `dependency_hashes` 覆盖或把 `run_id` 置空。
2. **D-14 版本语法不唯一、不严格。** `SEMVER_PATTERN` 用了 `\d`，在 Python 中会匹配
   Unicode 数字（`١.٠.٠` 被接受）；允许前导零（`01.0.0`）；不支持合法的 build metadata；
   prerelease 的数字标识符前导零与空标识符未被拒绝。`schema_version` 的 major 还用宽松的
   `int(value.split(".")[0])` 读取，与校验用的正则不是同一来源。
3. **D-15 v1 只读入口会给任意字典铸造 legacy 身份。** `read_v1()` 只检查模型名与 major，
   于是 `{"schema_version": "1.0.0"}`、错模型、缺必填字段、多余字段、
   甚至改了标签的 v2 载荷都能得到一个"看起来正经"的 v1 `content_hash`。
4. **D-16 JSON Schema 弱于运行时。** `dependency_hashes` / `plugin_versions` /
   `StrategyArtifact.dependencies` 在 Python 侧强制了键值格式，导出的 Schema 却只写
   `{"type": "object", "additionalProperties": {"type": "string"}}`，外部消费者无从得知规则，
   两套规则也会各自漂移。

## 决策

### D-13 公开复制更新必须重新走完整校验

`Contract.model_copy(update=...)` 改为：用 `{**self.__dict__, **update}` 重新构造并
**完整校验**，返回同一具体模型类型的实例。无 `update` 的普通 / 深复制保持 Pydantic 行为
（输入已经是校验过的实例）。因此复制更新与正常构造等价：映射字段仍是只读的 `FrozenMapping`、
与调用方输入断开别名，错误 `kind`、缺失依赖绑定、空 `run_id`、非法版本与未声明字段都被拒绝。

**可信边界（必须如实表述）**：`model_construct()` 是 Pydantic 面向**可信数据**的低层逃生口，
它按设计不做校验。它不是受支持的外部载荷入口，本项目也不为它提供安全承诺；
不得把"`model_construct` 也安全"写进任何文档或测试。受支持的入口是构造函数、
`model_validate` / `model_validate_json` 与本 ADR 定义的 `model_copy`。

### D-14 唯一、ASCII、完整 SemVer 2.0.0 语法

全项目只有一套版本语法，由 `core/domain/base.py` 的组件常量拼成：

| 规则 | 说明 |
|---|---|
| 字符集 | 只接受 ASCII：一律用 `[0-9]`，**禁止** `\d`（它会匹配 Unicode 数字） |
| core | `major.minor.patch`，各段为 `0` 或不带前导零的正整数 |
| prerelease | 可选 `-`；标识符不得为空；数字标识符禁止前导零 |
| build metadata | 可选 `+`；标识符为非空字母数字连字符 |
| major 读取 | 从**已经验证的**正则命名分组读取，禁止 `int(value.split(".")[0])` |

`schema_version`、`Ref.version`、`VersionedSpec.version`、`plugin_versions` 的键与
ref-keyed 依赖键**共用同一套组件**。`SEMVER_PATTERN` 全部使用非捕获组，可安全嵌入
Pydantic 与 JSON Schema；解析用的正则由同样的组件加命名分组构成。

契约的 `str_strip_whitespace=True` 会先剥离字符串字段的首尾空白，剥离后仍须是规范版本；
解析函数本身不接受带空白的版本串。

**同 major 更高 minor 的准确含义**：版本号**可识别**，但这不是前向兼容承诺。
载荷中出现当前实现未知的字段仍然 fail closed（`extra="forbid"`）。
不得声称"任意未来 minor 都能读"。

### D-15 v1 只读入口的顶层 shape gate

`read_v1()` 在计算任何哈希**之前**必须通过下列确定性检查，任一不满足即 `ContractViolation`：

1. 模型名在 `V1_MODEL_NAMES` 内；
2. 已提交的 `schemas/v1/<Model>.schema.json` 存在且可解析——**快照缺失时 fail closed**；
3. `schema_version` 是 v1 **已发布语法**的 `1.x.y[-prerelease]`（收紧为纯 ASCII、major 固定
   为 1）。刻意**不**套用 v2 的新语法：旧身份不能被新规则重写；
4. 快照 `required` 声明的顶层字段全部存在；
5. 快照声明 `additionalProperties: false` 时，拒绝未知顶层字段。

**限制（必须如实表述）**：这**不是完整的 JSON Schema 递归校验**。它不校验嵌套对象的结构、
类型、取值范围或数组元素，也**不引入 `jsonschema` 依赖**。它保证的是"这份载荷在顶层形状上
确实是该 v1 模型"，足以拒掉任意字典、错模型、缺必填、多余字段与改了标签的 v2 载荷——
这些都必须有**真实向量反例**作为验收证据。

### D-16 JSON Schema 必须表达运行时已执行的格式约束

`ReproducibilityTuple.dependency_hashes` / `plugin_versions` 与
`StrategyArtifact.dependencies` 的键值格式改为**标注在字段类型上**（`RefKey` / `PluginKey` /
`ContentHash`）。Pydantic 用同一个 pattern 字符串同时完成运行时校验与 Schema 导出，
因此**不存在两套来源**：

| 字段 | 键 pattern | 值 |
|---|---|---|
| `dependency_hashes` | `kind:name@semver`，`kind` 为已知取值的枚举式 alternation | 64 位小写 SHA-256 |
| `dependencies` | 同上 | 同上 |
| `plugin_versions` | `name@semver` | 同上 |

导出形式为 `patternProperties` + `additionalProperties: false`（`propertyNames` 的等价写法），
使 Schema 与运行时一样拒绝不匹配的键。

### 为什么版本号仍是 2.0.0

`2.0.0` **尚未发布**：没有合并进 `main`、没有 tag、没有远程仓库、也没有任何 v2 数据被登记
（仓库内无持久化的实验 / Run / Profile / 报告）。因此本 ADR 是对同一个未发布版本的**纠偏**，
不是新的破坏性变更，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。
若将来 `2.0.0` 已经 tag 或已有数据登记，同类收紧必须走新的 major。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| D-13 用 `model_config` 禁用 `model_copy` | 破坏 `LifecycleHistory.append` 等既有合法用法，且不解决"需要一条安全的复制更新路径" |
| D-14 只补 build metadata、保留 `\d` | 留下 Unicode 数字与前导零两个身份歧义入口 |
| D-15 引入 `jsonschema` 做完整递归校验 | 新增依赖，超出授权；顶层 gate 已能拒掉全部已知反例 |
| D-16 手写一份 Schema 后处理 | 会产生与运行时并行的第二套规则，正是要消除的漂移 |

## 后果

- 正面：公开构造路径唯一且一致；版本身份不再有 Unicode / 前导零歧义；旧载荷不能凭空获得
  legacy 身份；外部消费者能从 Schema 读到真实规则。
- 负面 / 代价：`model_copy(update=...)` 从 O(1) 写入变为一次完整校验（契约对象都很小）；
  `core/compat/v1.py` 依赖仓库中的 v1 快照文件。
- 对复现性的影响：**固定 v1 向量的旧哈希保持不变**；`schemas/v1/` 的 35 份快照逐字节不变。

## 验收矩阵

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | 14 个映射字段经 `model_copy(update=原始 dict)` | 仍是 `FrozenMapping`，与原始 dict 别名隔离，类型不变 |
| 2 | `model_copy` 写入错误 `kind` / 缺依赖绑定 / 空 `run_id` / 非法版本 / 未声明字段 | 全部拒绝 |
| 3 | 无 `update` 的 `model_copy()` 与 `model_copy(deep=True)`；`created_at` 复制；`LifecycleHistory.append` | 继续正常工作 |
| 4 | `0.0.0`、prerelease、build metadata、`1.0.0-rc.1+build.1` | 接受 |
| 5 | `01.0.0`、`1.0.0-01`、`1.0.0-`、`1.0.0+`、`1.0.0-alpha..1`、`١.٠.٠`、`1.0`、`1.0.0.0`、`v1.0.0` | 拒绝 |
| 6 | 同一语法作用于 `schema_version` / `Ref.version` / plugin 键 / 依赖键 | 一致接受与拒绝 |
| 7 | v1 读取：`{}`、`{"schema_version": "1.0.0"}`、错模型的真实向量、缺 required、多余顶层字段、改标签的 v2 载荷 | 全部拒绝 |
| 8 | v1 读取：`1`、`1.x`、`1.0`、`01.0.0`、`1.0.0.0`、`١.٠.٠`、`2.0.0` | 全部拒绝 |
| 9 | v1 读取：5 份固定向量、`1.4.0` 等同 major 更高 minor | 接受，且**旧哈希不变** |
| 10 | v1 快照目录缺失 | fail closed（拒绝） |
| 11 | 三类映射字段的导出 Schema | 有可机读的键 pattern 与 64 位小写 SHA-256 值 pattern，且禁止不匹配的键；与运行时同源 |
| 12 | 同 major 更高 minor + 未知字段 | 版本号可识别，但未知字段仍被拒绝 |
| 13 | `schemas/v1/` 35 份快照 | 逐字节不变 |

## 合规检查

- [x] 不修改 ADR-0008 / 0009 正文，也不改变其已批准的方向
- [x] 不修改 Validation Constitution 与 roadmap；动机与任何实验结果无关
- [x] Domain 层仍只依赖标准库与 Pydantic；未新增依赖、未安装软件
- [x] 未实现 Registry / Runner / 存储；未合并 `main`、未创建 tag
- [x] `model_construct` 的可信边界与 v1 gate 的限制均如实写明，未夸大
