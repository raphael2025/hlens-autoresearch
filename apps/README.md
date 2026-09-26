# apps/

Application Plane。包含 api、worker、web、execution。**运行时不得 import `research/`**（01-system.md §3）。

> 框架已实现（Raphael 2026-09-25 全阶段框架指示）：`api/`（ADR-0048）、`worker/`（ADR-0044）、`web/`（ADR-0048）、`execution/`（ADR-0046，仅模拟、无实盘能力）；均为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

- `promotion/`（ADR-0005，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：Equivalence Gate——在 artifact 的金标准输入上运行候选生产 `StrategyProvider` 并逐字节比较（契约无容差字段，故精确比较），产出 `EquivalenceCheck`；只有已登记的通过检查才生成 `DeploymentRecord`；来自 `research.*` 的候选被拒（`candidate_is_research_code`）；从不 import `research/`。见 [promotion/README.md](promotion/README.md)
