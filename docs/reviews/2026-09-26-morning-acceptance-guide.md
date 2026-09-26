# 早上验收指南（2026-09-26，给 Raphael）

对应指令："使用 4 个子代理加速开发，直到项目全部开发完成；先按框架实现所有代码，每一步更新文档，开发完成后再逐个调试"
与"继续完成剩下的所有，明早我来验收"。

## 1. 一句话结论

**全部 Phase 的框架代码已完成并通过严格全量门禁；第一轮调试已修复 24 个独立复核发现的问题（外加真实数据冒烟发现的 3 个）；
没有任何策略被验证或晋升，Profile 数值未冻结，系统没有下单能力。有 8 件事等你决定（§4）。**

## 2. 现在有什么

| 范围 | 状态 | 主要位置 | ADR |
|---|---|---|---|
| Phase 0.5 公共知识库 | 🧱 框架 | `core/contracts/knowledge.py`、`plugins/knowledge/` | 0034 |
| Phase 1 市场表示 | 🔄 实现与红队返修完成，**待 Codex / 你验收** | `infrastructure/` | 0021～0033 |
| Phase 2 市场状态 | 🧱 框架 | `core/contracts/state.py`、`plugins/states/`、`infrastructure/state/` | 0035 |
| Phase 3 事件与交互 | 🧱 框架 | `core/contracts/event.py`、`plugins/events/`、`infrastructure/event/` | 0036 |
| Phase 4 Outcome + 最小验证门 | 🧱 框架 | `core/contracts/outcome.py`、`plugins/outcomes/`、`research/validation/` | 0037 |
| Phase 5 策略库 + 回测 | 🧱 框架 | `core/contracts/strategy.py`、`plugins/backtest/`、`research/strategies/` | 0038 |
| Phase 6 状态 × 策略 | 🧱 框架 | `research/experiments/` | 0039 |
| Phase 7 假设生成 + LLM | 🧱 框架（LLM 只有离线替身，无密钥） | `research/hypotheses/`、`plugins/llm/` | 0040 |
| Phase 8 验证与稳健性（G4） | 🧱 框架 | `research/validation/` | 0041 |
| Phase 9 合成市场 + 校准 | 🧱 框架（校准只给证据，不选数值） | `plugins/synthetic/`、`research/synthetic_lab/` | 0042 |
| Phase 10 策略路由（纸面） | 🧱 框架 | `research/router/` | 0043 |
| Phase 11 持续研究循环 | 🧱 框架（真实组件、预算、永不到 ACTIVE） | `apps/worker/`、`research/loop/` | 0044 / 0049 |
| Phase 12 策略进化 | 🧱 框架 | `research/evolution/` | 0045 |
| Phase 13 生产（**仅模拟**） | 🧱 框架（实盘结构上被拒绝） | `apps/execution/` | 0046 |
| Phase 14 技术迁移 | 🧱 框架 | `infrastructure/migration/` | 0047 |
| 研究控制台 | 🧱 只读 API + 7 个网页，`npm run build` 通过 | `apps/api/`、`apps/web/` | 0048 |

🧱 = FRAMEWORK_IMPLEMENTED / NOT_VALIDATED：代码能跑、有测试，但没有在真实数据上验证过任何研究结论。

跨阶段接线已完成：特征 / 状态 / 事件 → 策略信号；状态 → 事件；回测 → 状态 × 策略 → 路由；策略 → 模拟执行（按权益 / 价格定量）；
经校验的数据集 → 标签与回测；持续循环使用真实组件（状态、完整 G0–G4、可复现实验记录、进化）；研究结果写入控制台。

## 3. 怎么验

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

最近一次严格门禁（ruff / format / mypy / `uv lock --check` / 全量 pytest，含 PostgreSQL 测试库）：`5432b2f` 上 5619 项全部通过（约 30 分钟）。
真实数据格式的端到端能力冒烟：`tests/infrastructure/e2e/test_research_pipeline_real_data.py`（需要测试库环境变量）。
持续循环端到端：`tests/research/loop/test_loop_e2e.py`（植入效应的假设在数据足够后通过 G0–G4 进入 OOS，纯噪声全部不通过，永不到 PAPER / ACTIVE）。

控制台（可选）：

```bash
cd apps/web && npm ci && npm run build
```

## 4. 需要你决定的 8 件事（都在 PROJECT_STATUS §6，不决定时系统保持保守）

| ID | 一句话 | 我的推荐 |
|---|---|---|
| D-FLOAT | 验证结果与 Profile 阈值在哈希里用浮点数，跨平台可能不一致 | 另起 ADR 改为精确小数 |
| D-PFIELDS | Profile 缺容量、跨资产、开封预算等字段 | 另起 ADR 只加字段，数值以后冻结 |
| D-CTRL | 同一个显著性阈值被"策略检验"和"负对照"反向使用 | 与 D-PFIELDS 一起，给负对照单独字段 |
| D-MINEFF | 条件假设的"最小效应"算假设内容还是验证门槛 | 算假设内容 |
| D-VFAIL | 生命周期没有 VALIDATION → FAILED | 另起 ADR 增加（需证据） |
| D-DEP | 研究循环依赖 `apps/worker`（反向不允许），我已依授权接受 | 维持 |
| D-PARTIAL | 回测按成交量上限部分成交时，冻结契约不允许把剩余量顺延到后面的 bar | 另起 ADR 扩展契约 |
| D-NET | 本机没有真实行情；要跑真实数据需下载公共归档（无密钥） | 授权 BTC / ETH 各 1～3 天 |

## 5. 调试第一轮做了什么

详见 [调试待办](2026-09-25-framework-debug-backlog.md)（A 节 R1～R26 与 E 节）。重点：

- 验证门：标签泄漏、样本数高估、封存样本外可反复开封 / 开封后可无限读、统计前未切分、G4 空配置或缺字段静默通过、
  容量"算出数字即通过"、walk-forward 重叠 / 空窗、CSCV 未按标签持有期 purge——全部修复并带回归测试。
- 开封必须逐个假设族由人批准；一次开封只能评估一次，中途失败也算用掉（记为 INCONCLUSIVE），且在进程重启后仍然有效（落盘账本）。
- 模拟执行：场所自身强制 Kill Switch；策略权重按权益 / 价格定量（不再把权重当数量）。
- 真实数据链：同一条研究链的特征与价格两个 manifest 须经 `pair_manifests` 校验配对，验证器 G0 核对标签、回测与特征来自同一对已校验数据集，对不上即 FAIL。
- 调试第二轮（2026-09-26 凌晨）：持续循环的全部状态（审计、Trial 账本、开封记录、谱系、审阅批准、失败登记）可放在同一个目录，进程重启后逐轮继续、结果与不中断运行完全一致，任一文件被删、被截断或被篡改即拒绝启动；交互事件的上游规格与输入来源逐项核对；数据集 bar 测试补齐 PostgreSQL 版本；控制台补齐全部报告种类的示例与校准证据页。
- 调试第三轮：状态目录绑定预算、开封配额、决策步长与 G4 参数（重开时任何改动即拒绝；提高预算须换新目录，由人决定）；可选目录外锚点识别整体截断；交互事件的上游哈希改为逐字段精确匹配，`require_full` 在证据缺失时拒绝。
- 调试第四轮：轮间人工批准即时写检查点并推进锚点；文件型持久事件总线（通过总线契约测试）；回测器可选的成交量上限 / 市场冲击 / 融资成本（默认结果逐字节不变）；循环审计记录成为版本化契约（ADR-0050，只追加，现有哈希不变）。
- 持续循环：在累积研究数据上验证，每次重新评估都算一次新 trial（多重检验校正随之增长）；已拒绝的不再评估。

## 6. Git 状态

- 工作分支：`claude/hlens-autorecearch-dev-c05c2b`；已快进备份到私有仓库 `wip/phase-1-unreviewed`。
- **没有**合并 `main`、**没有**打 tag、**没有** force push——这些需要你本人批准。
- 我没有做任何实盘、密钥、资金、Constitution、Profile 数值或冻结契约的改动。
