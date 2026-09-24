# ADR-0013: 确定性判定函数与数值合法性（D-19、D-20）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24 起草，等待 Codex 文档复核；未获批准，不得实施） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3） |
| 影响范围 | Contract / Validation |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0008](0008-contract-payload-immutability.md) |

## 背景

`07-validation.md` §1 规定"验证是确定性程序，不是 LLM 判断，也不是人工目测"。
当前契约离这条原则还有两处缺口：

1. **整体判定不是门结果的函数。** `ValidationReport._verdict_consistent`
   （`core/domain/research.py:308-313`）只拒绝"有 FAIL 却判 PASS"一种组合。
   因此"全部门 INCONCLUSIVE，整体 PASS"、"证据不足却判 PASS"、"全部 PASS 却判 FAIL"
   都能通过校验。这正是"证据不足即 PASS"这一风险的结构性入口：判定可以与证据脱钩。
2. **数值合法性只在哈希时把关。** `canonical_json`（`core/domain/base.py:248-257`）用
   `allow_nan=False` 拒绝 NaN / ±Infinity，但那是**计算内容哈希时**才发生的。
   一个 `value = nan` 的 `GateResult` 可以被正常构造、比较、序列化为非标准 JSON，
   只有在有人去算哈希时才报错。阈值与来源的配对也只检查了一个方向
   （`research.py:281-285`：有 `threshold` 必须有 `threshold_source`，反之不检查）。

`SignificanceParams.multiple_testing_threshold` 与 `overfitting_threshold`
（`core/contracts/validation_profile.py:92,94`）是无界 `float`，而同一文件中其它比例型字段
都有范围约束。

## 精确决定

### D-19.1 整体 Verdict 是门结果集合的精确确定性函数

`ValidationReport.verdict` 必须等于下列函数对 `gates` 的取值，**不等即拒绝**（不是"不得为 PASS"这类单向检查）：

```
任一门 FAIL            -> FAIL
否则任一门 INCONCLUSIVE -> INCONCLUSIVE
否则全部门 PASS         -> PASS
```

`gates` 至少一项（现状）。该函数是全函数：三种情形覆盖了所有可能的门结果集合。

**报告外因素必须表达为一个 `GateResult`。** 任何想让判定偏离上式的理由——证据不足、
数据质量不达标、样本量不够、人工保留意见、外部事件——都必须**物化为报告内的一个门**
（有 `gate_id`、`metric`、`value`、`verdict`），再由上式得出整体判定。
**不得**通过直接设置 `verdict` 来表达这些理由。这条规则是"证据不足不得等同于 PASS"的
可执行形式：没有对应的门，就没有对应的判定。

### D-19.2 `gate_id` 在每份报告 / 元数据内唯一

`ValidationReport.gates` 与 `ExperimentMetadata.gate_results`
（`core/contracts/profile_selection.py:101`）内部的 `gate_id` 必须互不重复。
重复的门意味着同一检查有两个结果，判定函数将不再良定义。

### D-19.3 threshold 与 threshold_source 成对出现

`GateResult` 中 `threshold` 与 `threshold_source` 必须**同时存在或同时缺失**：
有阈值必须声明来源（现状），有来源也必须有阈值（新增）。
无阈值的纯报告项（`reported_only_metrics`）两者都留空。

**路径真实性延期。** `threshold_source` 是否真的指向所绑定 Profile 版本中的某个字段、
其值是否等于 `threshold`，由验证服务在持有 Profile 实例时核验；契约层拿不到 Profile 实例
（元组里只有版本与内容哈希）。不得把格式检查表述为来源已核实。

### D-20.1 全局拒绝 NaN 与 ±Infinity

**所有 Contract 的所有浮点字段在校验阶段即拒绝 NaN 与 ±Infinity**，不再等到计算内容哈希时。
包括但不限于 `GateResult.value` / `threshold`、`ExperimentMetadata.realized_holding_stats`、
`ValidationProfile` 的各比例与倍数字段、`LifecycleParams.degradation_thresholds`、
`inconclusive_bands`（含映射与序列中的数值）。

序列化与哈希路径的既有约定不变且必须如实保持：`canonical_json` 的 `allow_nan=False`
保留，**不得**先把非法数值转成 `null` 再参与序列化或哈希（ADR-0008 决策 4）。
两处是同一条规则的两道关口，不是互相替代。

### D-20.2 两个概率型阈值的结构范围

`SignificanceParams.multiple_testing_threshold` 与 `overfitting_threshold` 是
**无量纲的 unit interval 量**，结构范围收紧为 `[0, 1]`（含端点）。

**本 ADR 不选择任何实际阈值。** 这是**结构上的合法取值范围**，不是校准值；
具体数值属于 D-09 的 TBD 系列，在 Phase 4 校准后写入具体 Profile 版本（ADR-0007 两步冻结 Step 2）。
端点保留是刻意的：`0` 与 `1` 是否算合理配置属于校准判断，不属于结构约束。

### D-20.3 完整门集合不在 DTO 中写死

一份报告必须包含哪些门（G0–G3、稳健性、封存 OOS 等）由**所绑定的 Validation Profile**
决定，并由验证服务检查。契约层**不**在 DTO 里写死任何门 ID 清单，也不要求某个 `gate_id` 必须出现。
理由：门集合随 Profile 与研究类别变化，写进冻结契约会造成契约与 Profile 双头管理。

## 明确不做

- 不选择任何验证阈值数值（D-09 的 TBD-1 ~ TBD-5 仍未批准）。
- 不定义门的计算方法、指标定义或 `metric` 的取值集合。
- 不规定报告必须包含哪些 `gate_id`（见 D-20.3）。
- 不实现验证流水线、验证服务或任何门的计算。
- 不改变 `Verdict` 枚举的取值。
- 不为 `inconclusive_bands` 定义语义（它如何影响门的判定属于验证服务与 Profile 校准）。
- 不引入浮点容差、舍入或跨语言数值规范化承诺（ADR-0008 决策 4 的边界不变）。

## 运行时延期义务

| 义务 | 说明 |
|---|---|
| 门集合完整性 | 报告是否包含 Profile 要求的全部门，由验证服务按 Profile 检查 |
| 阈值来源真实性 | `threshold_source` 指向的 Profile 字段是否存在、值是否一致 |
| 门计算正确性 | `value` 是否真的由声明的 `metric` 在正确数据上算出 |
| Profile 必须已 frozen | 绑定进实验的 Profile 状态检查（`06-experiment.md` §7，未实现） |
| 报告与运行的一致性 | `run_id` / `experiment_hash` 是否指向真实存在的运行 |

## Schema 与迁移影响

- 受影响模型：`ValidationReport`、`GateResult`、`ExperimentMetadata`、`SignificanceParams`
  （以及所有含浮点字段的契约模型的校验行为）。
- 两个阈值字段在 JSON Schema 中新增 `minimum` / `maximum`；`GateResult` 的配对规则是
  跨字段校验，JSON Schema 中不完全可表达，必须在文档与测试中写明。
- 需要重新导出 `schemas/`；实际差异以实施时的重导出结果为准。
- `schemas/v1/` 与 `tests/vectors/v1/` **逐字节不变**。
- 不新增契约模型，`V1_MODEL_NAMES` 不变。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
也没有任何 v2 数据登记。因此本 ADR 是对同一个未发布版本的收窄，
`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本矩阵是**未来实现批次**的验收条件，本轮只起草，未运行、未实现。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | 有 FAIL 门，整体判 PASS / INCONCLUSIVE | 均拒绝 |
| 2 | 有 FAIL 门，整体判 FAIL | 接受 |
| 3 | 无 FAIL、有 INCONCLUSIVE 门，整体判 PASS | 拒绝 |
| 4 | 无 FAIL、有 INCONCLUSIVE 门，整体判 FAIL | 拒绝 |
| 5 | 全部门 PASS，整体判 FAIL / INCONCLUSIVE | 均拒绝 |
| 6 | 全部门 PASS，整体判 PASS | 接受 |
| 7 | 三类门结果的全部组合穷举 | 判定与函数逐一相符 |
| 8 | 同一报告内两个相同 `gate_id`；`ExperimentMetadata.gate_results` 同样 | 均拒绝 |
| 9 | `GateResult` 有 `threshold` 无 `threshold_source` | 拒绝 |
| 10 | `GateResult` 有 `threshold_source` 无 `threshold` | 拒绝 |
| 11 | `GateResult` 两者都缺 | 接受 |
| 12 | `GateResult.value` / `threshold` 为 NaN、`inf`、`-inf` | **构造时**即拒绝（不依赖哈希路径） |
| 13 | 映射型浮点字段（`realized_holding_stats`、`degradation_thresholds`、`inconclusive_bands`）含非法数值 | 拒绝 |
| 14 | 非法数值经 JSON 文本载荷进入（`model_validate_json`） | 拒绝 |
| 15 | `canonical_json` 遇非法数值 | 仍然报错，且不产生 `null` |
| 16 | `multiple_testing_threshold` / `overfitting_threshold` 取 `-0.1`、`1.1` | 拒绝 |
| 17 | 同上取 `0`、`0.5`、`1` | 接受 |
| 18 | 契约中是否出现写死的门 ID 清单 | 不存在 |
| 19 | `schemas/v1/` 35 份快照与 v1 固定向量 | 逐字节不变，旧哈希不变 |

## 后果

- 正面：整体判定不能再与门证据脱钩，"证据不足即 PASS"在契约层被结构性排除；
  任何想影响判定的理由都必须留下一条可审计的门记录；非法数值在入口就被拒绝，
  而不是在事后算哈希时才暴露。
- 负面 / 代价：验证流水线必须把所有裁决理由物化为门，实现成本上升；
  现有测试与工厂中手工设置的 `verdict` 需要与门结果对齐。
- 对复现性的影响：v1 只读路径与旧哈希不受影响；仓库内无历史报告数据受影响。

## 合规检查

- [ ] 不修改 Validation Constitution 与 Validation Profile 的既有内容
- [ ] 不为提高回测表现而改变判定规则；动机与任何实验结果无关（H3）
- [ ] 不选择任何数值阈值（D-09 数值仍未批准）
- [ ] 不修改任何已批准 ADR 的正文
- [ ] 未实现验证流水线 / 验证服务
