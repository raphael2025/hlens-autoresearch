# ADR-0008: 契约载荷的只读表示与内容哈希载荷定义

| 字段 | 值 |
|---|---|
| 状态 | **Proposed** |
| 日期 | 2026-09-23 |
| 决策者 | Raphael（待批准） |
| 起草者 | Claude Code（Opus）起草；Codex 文档审查整理 |
| 相关 Phase | Phase 0 |
| 影响范围 | Contract |
| 是否破坏兼容 | **是**（语义变化 → 下一次未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`，与 ADR-0009 共同定义） |

## 背景

`core/domain/base.py:81` 的 `ConfigDict(frozen=True)` 只阻止属性重新赋值。所有映射类字段在实例创建后仍可原地写入，进而改变 `Contract.content_hash()`（`base.py:85-88`）的结果。受影响字段：`core/domain/specs.py:84,105,165,166,184`、`core/domain/research.py:102,103,104,112`、`core/contracts/validation_profile.py:134,162`、`core/contracts/profile_selection.py:91`、`core/domain/artifact.py:35`、`core/lifecycle/strategy.py:141`（共 14 处）。序列字段已是 `tuple`，嵌套 Contract 自身 frozen，故问题范围就是映射字段。这与 P1/P4（`00-overview.md:17,20`）、`02-domain.md:14,20`、Constitution A2 与 C-A5 直接冲突。

第二个问题：内容哈希的载荷未逐模型定义。`base.py:90-92` 用一个全局 `_non_semantic_fields()` 覆盖所有契约，而 `ValidationProfile.status`（`validation_profile.py:153`）是操作状态却计入哈希，导致同一份阈值内容在 `draft → frozen` 后哈希改变，`ReproducibilityTuple.validation_profile_hash`（`research.py:111`）指向哪一个无书面规定。

第三个问题：`canonical_json`（`base.py:66-70`）使用 `json.dumps` 默认参数（`allow_nan=True`、`default=str`），非标准 JSON 输出与"未知类型静默转字符串"两条路径均无书面约定。

## 决策

**1. 只读 Mapping 表示（不引入容器框架、不新增依赖）**

- 契约中的映射字段对外只暴露**只读 Mapping 视图**；底层可写字典不外泄。
- 构造期**隔离输入别名**：校验时基于调用方传入的对象构建独立副本，调用方保留的原引用不得影响已构造实例。
- **递归冻结内层**：嵌套映射同样转为只读视图，嵌套序列转为 `tuple`；嵌套 Contract 已 frozen，不再处理。
- **字段默认值走同一校验路径**（不得出现"显式传值被冻结、使用默认值未被冻结"的不一致）。
- 以**明确的 Pydantic 适配（自定义校验 + JSON Schema 保持 `object`）**实现；具体适配代码在获批后由实现批次给出，本 ADR 不预先断言其序列化输出。

**2. 不可变性的诚实边界**

本决策提供的是**契约使用层面的只读性**，用于阻止误用与意外修改。它**不**承诺抵御同进程内的恶意 Python（例如直接操作内部属性）。**不得**把它表述为"真正不可变"，也**不得**把深拷贝表述为不可变（深拷贝只切断外部别名，字段内部仍可写）。Python 对象的 `hash()` 与本项目的 `content_hash` 是两件事，不得混用。

**3. 内容哈希载荷：逐模型显式字段表**

把 `base.py:90-92` 的全局排除改为**逐模型显式声明的排除表**，基类默认仅排除 `created_at`（保持 `tests/test_contracts.py:136-144` 既有语义）。不做任何全局的 `*_id` / `recorded_at` / `occurred_at` 排除——`run_id`、`subject`、授权与审计时间可能是语义。

| 模型 | 排除字段 | 说明 |
|---|---|---|
| `Contract`（默认） | `created_at` | 维持现状 |
| `ValidationProfile` | `created_at`、`status` | `status` 是操作状态；`provenance` **保留**在哈希内 |
| 其余模型 | 沿用默认 | 不增加全局 ID/状态/审计时间排除规则；新增字段与 schema_version 变化仍自然影响内容哈希，不承诺 v1/v2 哈希相等 |

`ReproducibilityTuple.validation_profile_hash` 明确定义为"按 `ValidationProfile` 上述载荷计算的内容哈希"。**不新增** `profile_content_hash` 同义属性。因此 `draft` 与 `frozen` 版本的 `ValidationProfile.content_hash()` 在内容相同时相等——本 ADR 不同时主张"稳定"与"可以不同"。

**4. 哈希规范化约定（写入 `02-domain.md` §3）**

`canonical_json` 改为 `allow_nan=False`，移除 `default=str` 静默兜底（遇未知类型直接抛错）；模型哈希输入先经明确的 JSON 适配；键排序、分隔符、`ensure_ascii=False` 维持现状并固化为书面约定。所有参与内容身份的数值在序列化前拒绝 NaN/正负无穷，不得先转成 null 再参与哈希。

这只是 v2 明确的 Python/JSON 规范，不宣称已具备跨语言浮点规范化保证。JSON 往返、字典键顺序变化、Profile 状态变化与非法数值均提供固定测试向量；未来非 Python 实现必须通过这些向量和数值一致性评估。

**5. 版本与发布**

本 ADR 与 ADR-0009 **共同定义下一次尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`**。实现批次逻辑分开，但**中间不发布 v2、不登记 v2 实验**；版本号在两份 ADR 全部实施完成后一次性提升。**不承诺 `schemas/` 零 diff**：全局 `schema_version` 默认值变化会波及全部顶层与嵌套 Schema，实际差异以实施时重导出结果为准。

**6. 旧数据边界**

仓库内未发现持久化的实验、Run、Profile 或报告数据。**外部是否存在历史数据：NOT ENOUGH EVIDENCE**（Opus 本轮无 Bash，且未触碰旧项目，H13）。按 `10-migration.md` §3，v2 交付必须保留 **v1 只读路径**：v1 Schema 快照、可执行的只读读取入口及原哈希语义至少保留一个 major，仅保留说明文档不算完成读取兼容。

实现改动前用当前 v1 模型生成代表性固定载荷与哈希向量，作为兼容验收证据。读取 v1 不会自动补造 v2 缺失的依赖，也不会取得 v2 登记/晋升资格。未知 major 必须拒绝；新登记只走 v2 验证路径。**不得**静默把旧载荷按新算法重新计算并赋值。不建设数据库或在线迁移服务。

**7. 明确不实现**

Registry、数据库、内容寻址存储、跨进程强制、同 `name@version` 不同内容的拒绝逻辑（该拒绝是未来 Registry 的义务，本 ADR 仅写下条款）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（推荐）** 只读 Mapping + 别名隔离 + 递归冻结 + 逐模型哈希载荷表 | 覆盖赋值/`update`/`pop`/`clear`/`|=`/嵌套/别名/默认值；JSON 形状保持 `object` | 需要一处 Pydantic 适配；非内存级不可变 | — |
| B `dict` 子类覆写变更方法 | 实现直观 | 漏 `__ior__` 等入口；子类仍是可写 `dict`；默认值路径不受控 | 会造成"看似不可变"的错觉 |
| C 仅深拷贝载荷 | 改动最小 | 不解决问题：`spec.params["k"]=v` 仍成功 | 风险高于现状 |
| D 全部改为 `tuple[tuple[K,V], ...]` | 结构上不可写 | JSON 形状由 object 变 array，可读性与查询性下降 | 代价与收益不成比例 |

## 后果

- 正面：`content_hash` 成为可依赖的语义身份；Profile 状态流转不再污染内容哈希；规范化规则有书面约定。
- 负面 / 代价：依赖就地修改契约字段的代码将抛错（当前仓库无此用法）；新增一处 Pydantic 适配；必须持续如实说明只读性边界。
- 需要迁移的内容：重导出 `schemas/`（差异范围待实测）；保留 v1 只读 Schema 快照；`02-domain.md` §1/§3 文字同步。
- 对复现性的影响：仓库内无历史实验受影响；外部数据存在性证据不足，故**不宣称**迁移路径已验证。

## 合规检查

- [x] 已说明 major 版本与 v1 只读保留路径（`10-migration.md:26`）
- [x] 不修改 Validation Constitution；动机与任何实验结果无关
- [x] Domain 层仍无具体技术依赖（标准库 + Pydantic）
- [x] Research / Application Plane 边界不变
- [x] 不修改任何 Accepted ADR 正文

## 实现范围与验收

- 范围：`core/domain/base.py` 及上述映射字段所属模型、必要的 v1 只读兼容入口、相关契约测试与固定向量；Schema 导出注册和版本化快照；`02-domain.md` 的本 ADR 对应语义说明。
- 14 个现有映射字段均覆盖显式构造与默认值路径；赋值、删除、update、pop、popitem、clear、setdefault、原地合并、嵌套修改均不能改变对象。
- 修改构造时的原始字典或导出得到的普通 JSON 字典，不得反向改变原契约。
- JSON 往返保持规范化内容和哈希，往返后仍只读；对同一内容重复调用哈希稳定。
- 除 status 和非语义创建时间外内容相同的 Profile 哈希相同；参数或 provenance 变化必须改变哈希。两个 Profile 必须各自满足其状态校验，不绕过校验构造测试对象。
- v1 固定载荷能通过只读入口读取并保留原身份语义；不能作为 v2 新登记直接使用；未知 major 被拒绝。
- 与 ADR-0009 的最终发布验收合并检查 v2 版本、全部嵌套 Schema、四项工程检查。两份 ADR 未同时获批时，不启动相互依赖的实现批次。
