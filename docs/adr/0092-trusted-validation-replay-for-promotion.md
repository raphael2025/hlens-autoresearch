# ADR-0092: Promotion 的可信验证重放 Provider

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权；由 Codex 执行 |
| 起草者 | Codex / GPT-6-Luna 子代理 |
| 相关 Phase | Phase 1 开发批次（覆盖 P8 Promotion 验证义务） |
| 影响范围 | Research / Validation / Promotion |
| 是否破坏兼容 | 否（研究侧 Promotion API 增加可选依赖；缺失时新增拒绝） |

## 背景（Context）

`research.validation.verification.verify_report` 能检查报告与 Profile 的绑定、阈值来源和门集，但无法证明 `GateResult.value` 是由该门声明的 `metric` 计算所得。现有 G0 `reproducibility` 仅重跑实验并比较结果 hash；`InSampleInput.reproduce` 返回字符串 hash，不提供重跑期间完整的门值。`ValidationReport` 和 `PromotionEvidence` 也没有可信的重跑输出。

Promotion 必须在构建 artifact 前核验报告值。验证服务不能把调用方附带的一组 `GateResult` 当作可信重算结果。与此同时，G5 sealed OOS 每个假设族只能开封和评估一次（ADR-0041）；Promotion 不能要求第二次读取 sealed 数据。

## 决策（Decision）

1. **可信边界是宿主组合根。** `research.promotion` 定义 `ValidationReplayProvider` Protocol，并通过显式依赖注入调用。宿主组合根负责选择受信任的实现及其只读 replay source；Provider 不从 `StrategySpec`、`ValidationReport` 或 Research Agent 载荷中发现或选择。Python 进程内无法仅凭对象结构认证任意实现，也不引入签名、凭据或网络服务。Promotion 对 Provider 的信任是配置边界，不是密码学证明。调用方不得把任意返回报告值的适配器当作生产 Provider。

2. **请求及输出绑定。** Provider 接收 Promotion 正在核验的原始 `ValidationReport`、与报告 hash/ref 匹配的 `ValidationProfile`，以及报告 `experiment_hash` 对应的 `ExperimentSpec`。返回的不可变 replay 结果必须明确绑定：`report_id`、`report.content_hash()`、`run_id`、`subject`、`experiment_hash`、Profile ref 和 Profile content hash，并含完整重算门集合。Promotion 先核实这些绑定，再接受输出。

3. **重算规则。** 受信任 Provider 必须从报告绑定的实验、Profile 和固定复现输入重新执行适用的验证计算，不得抄录报告中的值。输出与报告的 `gate_id` 集合必须完全相同且无重复；逐门核对 `metric`、`value`，以及存在时的 `value_exact`。缺门、多门、metric 不同、值不同、绑定不符、Provider 缺失、异常或拒绝均失败关闭为 `report_value_not_recomputed`。这不改变门、阈值、成本、切分、指标或 Verdict 规则。

4. **检查顺序。** Promotion 继续先检查 Profile/阈值一致性，以 `report_threshold_mismatch` 拒绝；之后检查流水线门集，以 `report_gate_set_incomplete` 拒绝；仅此前两类检查通过后，才要求并调用 replay Provider。重算核验失败使用 `report_value_not_recomputed`。

5. **G5 one-shot 限制。** Provider 不得再次打开或评估 sealed OOS。只有宿主可信执行路径在首次 one-shot 评估时已经产生、并绑定到该报告的可信计算证据，Provider 才能重算/核验 G5。当前系统没有内建 trusted replay Provider，也没有持久化此类 G5 replay artifact；因此无 Provider 或 Provider 无法提供绑定的首次执行证据时，相关 Promotion 一律以 `report_value_not_recomputed` 拒绝。本 ADR 不新增 artifact 格式，不放宽 sealed OOS 单次预算。

6. **本轮范围。** 本轮实现 Provider Protocol、绑定结果结构、纯核验函数与 Promotion fail-closed 接线；测试中可用显式标注的 TEST ONLY Provider 验证接口与拒绝顺序。TEST ONLY Provider 不代表可用于真实 Promotion 的实现。提供可访问固定研究输入、执行既有验证管线并在首次 G5 执行时捕获可信证据的宿主实现，留待后续批次。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| Promotion 接收调用方给出的 `GateResult` / 值映射 | 改动少 | 值可由报告提交方任意伪造，不能证明重算 | 拒绝 |
| Promotion 自行实现每种 metric | 不依赖 Provider | 重复实现验证算法，易与验证流水线漂移；无法重开 G5 | 拒绝 |
| Promotion 自动二次读取所有原始验证数据（含 G5） | 表面上能完整重跑 | 违反 sealed OOS one-shot 约束；Promotion 当前也没有可重建的输入上下文 | 拒绝 |
| 宿主选择 trusted replay Provider，Promotion 校验显式身份绑定与全部门输出 | 复用宿主的确定性执行路径；职责边界清楚；可缺省拒绝 | 信任 Provider 的实现与配置；当前无生产 Provider，暂时没有策略可通过 Promotion | 选择 |

## 后果（Consequences）

- 正面：Promotion 不再只相信报告自报的门值；Provider 返回必须与报告、Profile、实验绑定且覆盖完整门集。
- 负面 / 代价：Provider 是受信任计算边界，类型与 hash 绑定不能证明它没有造假；当前无内建实现，缺 Provider 会拒绝所有 Promotion。
- G5 限制：不能二次读取 sealed window。宿主须在首次 one-shot 执行时产生可信、报告绑定的计算证据；未实现该来源时 G5 Promotion 被拒绝。
- 对复现性的影响：不改变旧报告、Schema、哈希或验证输出；只新增 Promotion 前置核验。

## 合规检查

- [x] 不破坏已冻结契约或 Schema。
- [x] 不修改 Validation Constitution / Profile 数值或验证规则。
- [x] Domain 层不新增具体技术依赖。
- [x] Research / Application Plane 边界不变；Provider 由宿主组合根注入。

## 参考

- ADR-0013：确定性 Verdict 与有限数值。
- ADR-0041：G5 sealed OOS one-shot 与稳健性验证。
- ADR-0086：Promotion 的报告门集完整性检查顺序。
- `docs/architecture/07-validation.md` §2.1。
