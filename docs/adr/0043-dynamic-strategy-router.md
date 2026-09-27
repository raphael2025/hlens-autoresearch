# ADR-0043: 动态策略路由框架（Phase 10，仅纸面）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 影响范围 | `research/router/`（研究代码，H5；路由器本身须经完整验证与 Promotion 才能进入生产侧） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 只能路由生命周期为 ACTIVE / PRODUCTION_CANDIDATE 的策略（roadmap P10 禁止使用未验证策略）；构造即拒绝。
2. t 时刻的权重由 **t 时刻已知**的状态查声明式路由表得到（未知状态走声明的回退，通常空仓）；每个状态的权重非负且合计 ≤ 1
   （无杠杆）。
3. 每次权重变化计换手 `Σ|Δw|` 并按声明的费率计切换成本（路由切换成本吞噬收益是主要失败模式）。
4. 本构建只驱动纸面 / 模拟运行（执行侧见 ADR-0046，无实盘）；与回测偏差的声明范围由 P8 验证给出（调试阶段接入）。

## 实施说明

- **接线（W1，2026-09-25）**：`research/router/paper.py::paper_run` 把 t 时刻的路由权重与各被路由策略的 P5 目标仓位（as-of
  `decision_time <= t`）组合为目标仓位，交给注入的 `BacktestProvider` 模拟；每次路由换手按 `switching_cost_rate × 换手 × 决策时净权益`
  在决策之后的第一个估值点从现金扣除（叠加在成本模型之上）。返回路由器**自身**的 `BacktestResult`（归属
  `research_router_paper@0.1.0`），使路由器成为可被验证的策略对象；`RouterPaperRun.run_hash` 绑定路由规格、状态结果、策略结果、
  gross / net 结果与切换成本明细。仍只是纸面 / 模拟；无契约变化、无阈值。语义细节见 `research/router/README.md`。
- **证据模式的反向对照项（B58，2026-09-27，P10）**：`research/router/evidence.py::check_report` 在市场基准项之后新增最后一项检查：
  报告所用 Profile 的 `benchmark.inverse_control_reported` 为 true 而报告没有 ADR-0060 的 `G2.inverse_control` 门（逐字精确）→
  `inverse_control_missing`（`EligibilityRefusal` 新值；`RouterEligibilityRefused` / `RouterStop` 照常记录，拒绝码进入 `stop_hash`）。
  只要求存在（只报告项，无新阈值、不改判定）；为 false 时行为不变。信任模式哈希不变；无契约 / Schema / Profile 数值变化。
  控制台 `routerEligibility.ts` 增加该码的中文说明（G5 显示为「通过」）。Promotion（`research/promotion/service.py`）的同类检查已于 B59 补上（ADR-0005 补记）。
- **Profile 冻结登记边界（B62，2026-09-27，Codex 依 Raphael 授权）**：P10 证据模式**不**要求 Profile 的 `status = FROZEN`，也不读取
  `ProfileFreezeRegistry`。它核验研究层报告、Profile 身份 / 内容哈希、PASS、G5 与 Profile 声明所要求的 G2 报告项；冻结登记是 Promotion
  （ADR-0005 / ADR-0062）的权威门，不重复塞入纸面路由资格检查。证据模式的 `ACTIVE` / `PRODUCTION_CANDIDATE` 生命周期映射仍是调用方声明，
  其通过仅代表报告满足研究层路由前提，**不**构成 Profile 已冻结、策略已获 Promotion 或生产资格的证明。实盘 / 部署仍须走独立 Control Plane；本决定不放宽任何生产门。
