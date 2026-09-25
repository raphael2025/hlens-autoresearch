# ADR-0039: 状态 × 策略研究框架（Phase 6）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 影响范围 | `research/experiments/`（研究代码，H5；无契约变化） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 策略在 `(t, t_next]` 的收益归属于 **t 时刻已知**的状态（`StateResult` 在 t 的取值，只用 t 可见输入）；未知状态单列、不丢弃；
   没有状态评估的时刻即拒绝。
2. 输出每个状态的样本数、均值、胜率、合计，以及"最佳状态占全部盈利的比例"与"最佳状态内前 k 笔的占比"——即 Constitution
   C-R2 的输入；本框架**不**设阈值，判定属 Validation Profile（P4 / P8）。
3. 每个被研究的条件化变体都以 `conditioning` 假设登记进 `TrialLedger`，计入 trial 数（C-T1，roadmap P6 验收）。
4. 策略收益目前以"按时刻的收益序列"这一窄接口输入；接入 P5 回测结果与 P4 / P8 验证后形成完整实验（调试阶段）。
