# research/

Research Plane。探索性研究代码与实验编排。**研究代码不得直接成为生产代码**（CLAUDE.md H5），只能经 Promotion 流程。
`apps/` 及生产包不得 import 本目录（`tests/test_architecture_boundaries.py`）。

> 全阶段框架实现（Raphael 2026-09-25 指示）：下表各子目录的代码状态均为 **FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**；
> 验证阈值与 Profile 数值一律 TBD，由 Validation Profile 提供，代码中不固化任何数值。

## 当前内容

| 子目录 | Phase / ADR | 内容 |
|---|---|---|
| `features/` | Phase 1 F4（ADR-0030） | 仅规划说明（首批特征实现在 `plugins/features/`，执行器在 `infrastructure/feature/`） |
| `states/` | Phase 2（ADR-0035） | 状态稳定性诊断（分布、持续时间、转移矩阵、标签闪烁），见 [states/README.md](states/README.md) |
| `events/` | Phase 3（ADR-0036） | 事件频率、共现、领先滞后与重叠诊断（只描述，无阈值） |
| `promotion/` | ADR-0005 Promotion | 由验证证据（G0–G4 PASS + G5 PASS）、PAPER 及以后的生命周期与研究 provider 计算的金标准数据构建 `StrategyArtifact`；缺任何证据即类型化拒绝，今天所有库策略都被拒（`no_validation_report`）。见 [promotion/README.md](promotion/README.md) |
| `outcomes/` | Phase 4（ADR-0037） | Outcome 物化：只在标签已知后返回的标签表 |
| `validation/` | Phase 4 / 8（ADR-0037 / 0041） | Validation Pipeline：G0–G3、封存样本外门、purge / embargo 切分、负对照、成本；Phase 8 稳健性扩展 |
| `strategies/` | Phase 5（ADR-0038） | 研究策略库：时间序列动量、波动率目标风控、策略 → 风控 → 回测 → 验证 → Failure Registry 流水线 |
| `experiments/` | Phase 6（ADR-0039） | 状态 × 策略矩阵（接 P5 回测）与条件假设登记 |
| `hypotheses/` | Phase 7（ADR-0040） | 假设 DSL 组合算子、预登记 Trial 账本、知识 / LLM 草稿生成（LLM 草稿须审阅） |
| `synthetic_lab/` | Phase 9（ADR-0042） | 合成市场上的检测器校准（假阳性率与检出力）；验证门校准 harness（候选 Profile × 门的证据，不是 Profile 决定），见 [synthetic_lab/README.md](synthetic_lab/README.md) |
| `router/` | Phase 10（ADR-0043） | 动态策略路由（仅纸面）与纸面运行，见 [router/README.md](router/README.md) |
| `loop/` | Phase 11（ADR-0049） | 持续研究循环的六个研究阶段与组合根（通用调度 / 预算 / 生命周期守卫 / 审计在 `apps/worker/loop.py`），见 [loop/README.md](loop/README.md) |
| `evolution/` | Phase 12（ADR-0045） | 变异 / 组合 / 退役算子与谱系图（新对象必须新版本并重新验证） |
| `persistence/` | ADR-0040 / 0041 / 0045 实现说明 | 哈希链、只追加的本地 JSONL 日志（`AppendOnlyJournal`）：开封记录、Trial 账本与谱系图可选持久化，重启后仍只能开封一次、计数延续；链断裂或篡改即拒绝 |
| `reports/` | ADR-0048 report writer | 研究侧报告写入方：验证报告 / 循环轮次审计 / 状态 × 策略矩阵 / 路由纸面运行 → `apps/api` 只读控制台能读回的 JSON 文件，见 [reports/README.md](reports/README.md) |

跨阶段端到端冒烟：`tests/research/test_cross_phase_e2e.py`（合成市场 → 特征 → 状态 → 策略 → 回测 → 状态 × 策略矩阵 → 路由纸面运行）。
