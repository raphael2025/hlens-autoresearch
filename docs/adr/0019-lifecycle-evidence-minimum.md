# ADR-0019: 生命周期证据的最小结构（D-27）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24；待 Codex 复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 的项目技术决策授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 0（批次 C2） |
| 影响范围 | Lifecycle / Contract |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见「版本」一节） |
| 前置 | [ADR-0006](0006-strategy-lifecycle.md)、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md) |
| 来源 | [Phase 0 关闭复审 C1](../reviews/2026-09-24-phase0-closing-review-c1.md) F5（P2） |

## 背景

ADR-0006 §3 第 4 条规定："每次转移追加一条记录（from、to、时间、触发者、**证据引用**、批准人），
永不修改或删除。"但 `LifecycleTransition.evidence`（`core/lifecycle/strategy.py:117`）的类型是
`tuple[str, ...] = ()`：可以为空，元素也可以是空白字符串。

C1 复审的探针证实：一条 VALIDATION → OOS 转移在 `evidence = ()` 时被接受；
OOS → PAPER 由同一个标识触发并批准（`approved_by == triggered_by`）也被接受。

两件事的依据强度不同：

1. **证据引用**是 ADR-0006 已写明的记录组成部分，契约层能够、也应当保证它在结构上存在。
2. **职责分离**（触发者 ≠ 批准人）在已接受的 ADR 中**没有**作为规则出现；ADR-0006 Q-5 仍允许人
   直接执行部分退役，而且两个载荷字符串不相等也不能证明真实的职责分离。

## 决策（D-27）

### D-27.1 `LifecycleTransition.evidence` 至少一项且每项非空

- `evidence` 必须**至少一项**。
- 每一项在去除首尾空白后仍须**非空**。
- 本约束适用于**所有**合法转移，不只晋升边：预登记、运行完成、门结果、监控突破、拒绝、失败、退役
  都应有可追溯的依据。例如 IDEA → REJECTED 引用重复性或不可证伪的判断依据，CANDIDATE → FAILED
  引用出错的 Run，ACTIVE → DEGRADED 引用监控突破的证据，退役引用 Revalidation 报告或人工决定记录。
- 契约层只约束**结构**：证据项是字符串引用，其格式（报告 ID、Run ID、URI 等）本 ADR 不冻结。

### D-27.2 不要求 `approved_by != triggered_by`

本 ADR **不**在契约层加入职责分离规则，理由必须如实表述：

- ADR-0006 Q-5 尚未决定，草案允许人工直接执行 ACTIVE → RETIRED、PAPER → RETIRED；
  若契约层强制"触发者 ≠ 批准人"，等于替 Q-5 做了决定。
- `approved_by` 与 `triggered_by` 只是两个载荷字符串。二者不相等**不能证明**真实的职责分离
  （同一个人可以写两个不同名字）；二者相等也不一定是违规。把这种检查放进 DTO，
  会制造一个看似有防护、实则无法核验的自报控制点——与 ADR-0011 删除 `live_execution_enabled`
  的理由相同。
- 需要人工批准的转移集合仍按已接受的 ADR（ADR-0006 §3 第 6 条、ADR-0011 D-17.1）执行，不变。
- `approved_by` 的**真实性与权限**（是否是真实的人、是否有权批准该转移）由未来的授权服务核验。

### D-27.3 不校验证据的存在性与内容

契约层**不**校验证据引用指向的对象是否存在、是否可取回、内容是否支持该转移的结论；
这些是 Registry / Control Plane 的义务（ADR-0011「运行时延期义务」中"证据引用可取回"一项）。
本 ADR 只保证"有非空的证据引用"这一结构。

## 明确不做

- 不增删状态机的任何边，不改变终态集合，不改变人工批准转移集合。
- 不解决 Q-4 / Q-5 / Q-6 / Q-7。
- 不引入职责分离、双人批准或任何自报的"已审阅"标志。
- 不冻结证据引用的格式，也不把它改为 `Ref` 或其它结构化类型。
- 不收紧 `FailureRecord.evidence`、`RetirementRecord.evidence` 或其它模型（不在本裁决范围）。
- 不实现 Registry、授权服务或审计日志。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** evidence 至少一项且每项非空；不做自报职责分离 | 把 ADR-0006 已写明的记录组成部分落成可执行约束；不预判 Q-5 | 证据真实性仍依赖未来服务 | — |
| B 只对需人工批准的转移要求 evidence | 改动面小 | 拒绝、失败、劣化同样需要可追溯依据；与 ADR-0006 §3 第 4 条"每次转移"不符 | 覆盖不足 |
| C 同时要求 `approved_by != triggered_by` | 表面上更严格 | 预判 Q-5；自报字符串无法证明职责分离，制造虚假防护 | 见 D-27.2 |
| D 把 evidence 改为结构化引用（`Ref` / `ContentBlobRef`） | 可机读 | 证据种类与格式尚未确定；超出 Phase 0 必要性 | 冻结未理解的东西 |
| E 不改 | 零成本 | 与 ADR-0006 §3 第 4 条不一致；发布后再收紧必须升 major | 已有明确依据 |

## Schema 与迁移影响

- 受影响模型：`LifecycleTransition`，以及内嵌它的 `LifecycleHistory`。导出 Schema 中 `evidence`
  预期新增 `minItems: 1` 与元素的 `minLength: 1`；实际差异以实施时的重导出结果为准。
- 不新增契约模型，`CONTRACT_MODELS` 数量不变（38），`V1_MODEL_NAMES` 不变。
- 测试工厂与既有测试中不带证据的转移需同步补上证据（只存在于测试，不构成任何建议格式）。
- `schemas/v1/`（35 份）与 `tests/vectors/v1/`、`tests/vectors/v1_coverage/` **逐字节不变**；
  v1 只读路径不受影响。
- 仓库内无任何生命周期历史数据受影响。

### 版本（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，无任何 v2 数据登记。
因此本 ADR 是对同一个未发布版本的收窄，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。
**发布后做同类改变必须升 major。**

## 实施验收矩阵

> 本 ADR 尚为 Proposed；下列矩阵是获批后**实现批次**的验收条件。先写能暴露原缺陷的测试并记录 red。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | 任一合法转移 `evidence = ()` | 拒绝（Python 与 `model_validate_json` 两条入口） |
| 2 | `evidence` 含空串或纯空白项 | 拒绝 |
| 3 | 每一条 `ALLOWED_TRANSITIONS` 的边，带一项非空证据 | 接受（逐边覆盖，不只晋升边） |
| 4 | `LifecycleHistory` 直接构造与 `append`，其中一条转移证据为空 | 拒绝 |
| 5 | `approved_by == triggered_by` 且证据非空、批准要求满足 | **接受**（D-27.2：不做自报职责分离） |
| 6 | 需人工批准的转移缺 `approved_by` | 仍拒绝（既有规则不变） |
| 7 | 状态机转移集合与人工批准集合 | 与 ADR-0006 / ADR-0011 逐条一致，不变 |
| 8 | 证据引用指向不存在的对象 | **接受**（存在性属 Registry，D-27.3） |
| 9 | 导出 Schema 中 `LifecycleTransition.evidence` | 带 `minItems: 1` 与元素非空约束；current Schema 与重导出逐字节一致 |
| 10 | `schemas/v1/`、`tests/vectors/v1/`、`tests/vectors/v1_coverage/` | 逐字节不变，旧哈希不变 |
| 11 | 四项工程检查 | 实际运行且全绿 |

## 后果

- 正面：每一条生命周期记录都至少指向一份依据，ADR-0006 §3 第 4 条第一次在契约层可执行；
  不预判 Q-5，也不制造无法核验的职责分离假象。
- 负面 / 代价：调用方构造任何转移都必须提供证据引用；职责分离与证据真实性在授权服务落地前
  仍只能由人与流程保证。
- 对复现性的影响：无既有生命周期数据受影响；v1 只读路径与旧哈希不变。

## 合规检查

- [ ] 契约变更经本 ADR 提出，获批前不实施
- [ ] 不修改 ADR-0006 已冻结的状态机与人工批准集合
- [ ] 不修改 Validation Constitution 与 roadmap
- [ ] 不修改任何已接受 ADR 的正文
- [ ] Domain 层仍只依赖标准库与 Pydantic；未引入自报布尔标志
