# ADR-0069：Phase 12 `combine` 对风险与适用范围冲突失败关闭

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | Codex，依 Raphael 对本轮模块开发与决策的明确授权 |
| 相关 Phase | Phase 12 |
| 影响范围 | `research/evolution/combine`；不改 StrategySpec 契约或 Promotion / Validation |
| 兼容性 | 无 Schema / contract / hash 格式变化；此前会静默择一的冲突输入改为显式拒绝 |

## 背景

ADR-0045 允许两个策略父代组合为新版本，参数冲突须拒绝并保留双亲谱系。当前 `combine()` 会对同名但不同搜索空间定义静默使用第二方，也会通过 `first.risk_policy or second.risk_policy` 和 `first.applicable_instruments or second.applicable_instruments` 隐式选择一方。风险政策与适用范围会影响策略安全边界，ADR-0045 未授权这种择一行为。

## 决策

1. 父代必须为不同 `StrategySpec.ref`。冲突的同名 `params` 继续拒绝。
2. 若父代声明了同名 `param_search_space`，其值列表必须完全相等；不一致即拒绝。合并后的参数必须仍在合并后的对应声明空间内；不为缺少的参数空间补默认值。
3. 两个父代的 `risk_policy` 必须完全相等；`None` 与非 `None`、不同 ref 均为冲突并拒绝。组合不推断、择一或合并风险政策。
4. 两个父代的 `applicable_instruments` 必须完全相等，包括 `None`、空值与非空值的差异；不隐式取并集、交集或任一父代。不同适用范围拒绝组合。
5. 对以上约束一致的父代，维持既有确定性 signal union、参数合并及双亲 lineage。后代仍从 `IDEA` 开始完整重新验证；本 ADR 不触发循环内替换、生命周期推进、晋升或额外搜索。
6. 失败在创建 child 前发生，不登记、不运行 trial，也不修改任一父代。错误说明指出冲突字段，供调用方显式另建策略规格。

## 兼容性与实施

不修改 `StrategySpec`、Schema、哈希规则或 ADR-0045 的已有谱系规则。输入完全一致时，既有 child 字段和值保持不变；此前被静默择一的歧义输入现在抛出 `EvolutionError`。实现限于 `research/evolution/operators.py` 与其模块说明；测试工作留待后续统一验收批次。

## 结果

`combine` 只组合拥有一致风险边界、适用范围与参数搜索声明的父代；未定义的冲突一律 fail closed。实现状态为 `CODE_COMPLETE / DEBUG_PENDING`，不代表 Phase 12 验收。
