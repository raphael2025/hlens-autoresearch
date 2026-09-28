# 待决策清单（2026-09-28）— 供 Codex 审核与决定

> 起草：Claude Code（PM）。用途：把“代码补全轮次”中无法靠写代码推进的事项集中成一份决定包，交 Codex 审核。
> 基线：`phase/1-foundation-completion@c752d55`（本地，未推送）。所有代码测试均**未运行**。

## 0. 决策权边界（依 CLAUDE.md §0 / §3）

- **Codex 可决定**：在不修改冻结契约（`core/domain`、`core/contracts`、`docs/architecture/02-domain.md`）、Constitution、Validation Profile 数值 / 验证阈值的前提下，批准 ADR、定义模块语义、划定实现批次、复核并推送 phase 分支（§10.8）。
- **Raphael 保留**：Constitution 原则变化、改冻结契约（需 ADR + Raphael）、Profile 数值（H2/H3）、环境 / 数据库变更与安装（H12）、实盘 / 资金 / 风险预算、删除历史数据、合并 `main`、打 tag。
- 每项标注：**[Codex]** 可直接决定；**[Codex→Raphael]** Codex 先判定，若触及冻结契约则升级；**[Raphael]** 只能由 Raphael 决定，Codex 可给建议。
- 所有未决定项在代码中均保持 fail closed，不会产生错误结果。

## 1. 总表

| ID | 问题 | 决策权 | 阻塞的代码 | PM 推荐 |
|---|---|---|---|---|
| R-0 | 复核本轮 Codex 自批的 ADR 与提交，推送 phase 分支 | [Codex] | 无（质量关口） | 复核后推送 |
| D-P7-OPS | P7 五类组合算子语义 | [Codex→Raphael] | P7 算子 lowering | 逐项见 §3 |
| D-P11-AUTH | P11 非 synthetic 循环的权威来源 | [Codex]，部分依赖 D-LIST | P11 resolver | 见 §4 |
| D-P11-WINDOW | 滚动循环与固定日历 Profile 的配合 | [Codex] 维持现状；改 Profile 结构需 [Raphael] | P11 长期运行 | 维持固定日历 |
| DQ-9 | ADR-0077 chunk / leaf / fanout 参数 | [Codex]，需调试阶段容量证据 | 无（参数显式注入） | 调试阶段决定 |
| D-CATALOG-TABLES | 在真实 Catalog 创建 `event.*` / `state.*` 表 | [Raphael]（H12） | 无（运行操作） | 调试阶段一并授权 |
| D-UVICORN | 安装 `uvicorn` 可选依赖并本机运行 API | [Raphael]（H12） | 无（代码已完成） | 调试阶段授权 |
| D-LIST | 标的上市历史 / ADR-0051 | [Raphael]（已明确暂缓） | 真实数据集的标的池 | 仍暂缓，调试后再议 |
| D-09 | Validation Profile 五类数值 | [Raphael]（H2/H3） | 合规运行配置、Profile 冻结 | Phase 4 校准后冻结 |
| P14-TARGET | 迁移目标系统与具名 golden data | [Raphael]（需提供输入） | P14 target adapter | 有目标时再做 |
| P05-REVIEW | P0.5 种子 tags / assets 具名人工审阅 | [Raphael]（指定审阅者） | P0.5 验收 | 调试阶段安排 |
| MERGE | `phase/1-foundation-completion` 何时并入 `main` | [Raphael] | 无 | Phase 验收后 |

## 2. R-0 复核本轮 Codex 自批决定

以下 ADR 均由 Codex 依 Raphael 2026-09-28 授权自行起草并接受，建议 Codex 以独立视角再审一次（Claude sonnet / Cursor 已做部分复核）：

| ADR | 内容 | 提交 | 已知复核结果 |
|---|---|---|---|
| 0075 A1 / 0076 | E1 有界快照扫描、Canonical 定长摘要 | `5332034` | Cursor CR-1：未发现问题 |
| 0078 | P7 lowered outputs 权威全集 | `79ea546` | Cursor CR-1：未发现问题 |
| 0079 | Paper deviation 绑定 P8 声明范围 | `1974610` + 修复 `ecf8b3b` | CR1-2（Ref 信封比较）已修 |
| 0081 | 报告 payload 版本化 DTO | `c752d55` | 复核进行中（R1） |
| 0082 | P7 `interaction` 语义（其余 OPEN） | `29763f3` | 复核进行中（R1） |
| 0083 | P7 失败轮次 durable 重试协议 | `f7d3d07`、`0181514` + 续写中 | 复核进行中（R1） |
| 0080 | P11 权威解析 → BLOCKED | `b7ee4b1` | — |

需要 Codex 确认：
- 上述决定是否都在授权边界内，特别是没有变相修改冻结契约或验证语义；
- 是否同意把本轮已复核的提交推送到远端 `phase/1-foundation-completion`（§10.8；当前远端只有 `main`，这是新建远端分支）。

## 3. D-P7-OPS：P7 五类组合算子语义

来源：[ADR-0082](../adr/0082-p7-operator-semantics-and-lowering.md) 表格、[ADR-0068](../adr/0068-phase7-typed-operator-plans.md)、[ADR-0078](../adr/0078-p7-lowered-output-completeness.md)。`interaction` 已完成；其余五项 `operator_open`。任何一项接受后，`TypedPlan.runnable` 仍保持 False，是否启用运行另行决定。

**先决判断（Codex）**：`conditioning`、`ensemble`、`negation` 的名义输出都是 StrategySpec，而 ADR-0082 已指出“现有规格不能无损表达”（StrategySpec.signals 不能引用 StrategySpec，也没有状态门控字段）。若必须给 StrategySpec 增字段或新增规格类型，就属于冻结契约变更，须升级 Raphael。`temporal`、`transformation` 的输出是 EventSpec / FeatureSpec，可能在现有契约内完成。

| 算子 | 未定义点 | 选项 | PM 推荐 |
|---|---|---|---|
| `temporal` | “N bars 内”如何映射到时间轴 / bar 规格 / 日历；边界与端点可见性 | A：要求两个输入 EventSpec 声明同一 bar 规格，窗口按该 bar 计数，右端点含、左端点不含，结果可见时间 = 第二事件可见时间。B：统一改为显式时间长度（微秒），与 ADR-0061 seq DSL 对齐 | **A**，规格不一致时拒绝 |
| `transformation` | 统计总体、窗口、拟合区间、分位算法、缺值、训练期（Constitution C-L3 要求只用训练窗口拟合） | A：先只定义时间序列类（`standardize` / `difference` / `smooth`）：必须显式窗口、只向后看、拟合参数只来自绑定的训练窗口、缺值传播为缺失；`rank` / `quantile` 横截面类保持 OPEN。B：五种全部定义 | **A**，分步收敛 |
| `conditioning` | 状态不匹配 / 未知标签、门控方式、按状态 trial 与报告如何映射 | A：状态等于给定值时持有原策略目标仓位，否则空仓；未知状态视为空仓；每个 (策略, 状态值) 计一个 trial。B：保持 OPEN | 若现有 StrategySpec 可表达则 **A**；否则升级 Raphael（契约变更） |
| `ensemble` | 信号到仓位、投票 / 加权、平局、异步信号、风险政策、标的、成本 | A：成员目标仓位等权平均（不是投票），成员必须风险政策与适用标的完全一致（沿用 ADR-0069 combine 规则），成本按合成后的净仓位变化计算。B：保持 OPEN | 需契约支持成员引用；多半须升级 Raphael |
| `negation` | 反号对象（信号 / 仓位 / 交易）、现金 / 杠杆 / 成本、对照资格 | A：目标仓位取反，风险政策不变，成本按取反后的交易计算；**不**作为验证负对照（负对照由验证层定义）。B：保持 OPEN | 同上：需契约支持则升级 Raphael |

## 4. D-P11-AUTH：P11 非 synthetic 循环的权威来源

来源：[ADR-0080](../adr/0080-p11-authority-resolution.md) §“重开所需输入”，以及 ADR-0049 / 0067 / 0074。当前循环只运行 synthetic，缺权威时拒绝。

| 子项 | 问题 | 选项 | PM 推荐 |
|---|---|---|---|
| AUTH-1 生命周期权威 | 当前 ACTIVE 集合从哪里读；追加、排序、回滚检测、并发写、DEGRADED / RETIRED 后移出 | A：新增 durable 生命周期登记（复用 `research/persistence/journal.py` 的哈希链只追加日志），ACTIVE 集合 = 重放结果，所有转移经现有 `core/lifecycle` 状态机校验。B：从 promotion 服务记录派生 | **A**（不改 core，只加 infrastructure / research 登记） |
| AUTH-2 真实数据源 | source 身份、revision、可用时间、冲突时如何拒绝；与 DatasetBuilder 的关系 | A：唯一真实来源 = ADR-0077 v3 Dataset evidence manifest，source 身份 = manifest 内容哈希，可用时间沿用 ADR-0023 / 0032。B：另建 source registry | **A**；但真实数据集仍受 D-LIST 阻塞，在那之前只能用已下载样本 |
| AUTH-3 metric 定义 | 每个 metric 的字段、单位、精确算法、窗口、缺失处理、provenance | A：沿用 ADR-0067 degradation 报告已有指标，逐个写明算法与版本；不引入阈值（门槛属于 Profile）。B：新增指标集 | **A**；如需门槛数值则属于 D-09 |
| AUTH-4 入口 | 哪个调用方可触发非 synthetic 循环 | A：只允许显式 CLI 或函数入口，不接调度器，且不能突破 ADR-0074 的 synthetic-only operator | **A** |

AUTH-1、AUTH-3、AUTH-4 可由 Codex 直接决定并派实现；AUTH-2 的真实数据可用性受 D-LIST 限制。

## 5. D-P11-WINDOW：滚动循环与固定日历 Profile

来源：ADR-0049 实施记录遗留项（AUD-2b）。Profile 数据切分是固定日历，持续循环需要滚动窗口；换窗口意味着换 Profile。

- A：维持固定日历。循环只在已冻结 Profile 的窗口内运行，换窗口需要新 Profile，由 Raphael 冻结。无需代码或契约变更。
- B：引入“滚动 Profile”结构。属于 Profile 结构变更，需要 Raphael。

**PM 推荐 A**，由 Codex 记录为既定边界；B 等 D-09 冻结之后再议。

## 6. 只能由 Raphael 决定的事项（Codex 可附建议）

- **D-CATALOG-TABLES**：ADR-0066 已提供默认无副作用、`--apply` 才建表的显式命令；授权即可执行，无需写代码。建议调试阶段一并授权。
- **D-UVICORN**：代码与依赖声明已完成（ADR-0063），只差安装与本机运行，两项真实 Uvicorn 测试目前跳过。建议调试阶段授权。
- **D-LIST / ADR-0051**：Raphael 于 2026-09-26 明确“后面再授权”。它阻塞真实数据集的标的池，从而影响 AUTH-2 和真实数据回测。
- **D-09**：Profile 五类数值须经 Phase 4 校准后冻结；在此之前没有合规的运行配置（ADR-0074 operator 也因此不能生成合规配置）。
- **P14-TARGET / P05-REVIEW**：需要 Raphael 提供迁移目标与 golden data，并指定具名审阅者。
- **MERGE**：phase 分支并入 `main` 须等 Phase 验收，并由 Raphael 批准。

## 7. 不需要决定、但需知悉

- **E1-CAP-1**：容量门仍阻断。本轮只实现了有界代码，没有任何容量证据；ADR-0077 契约层正在实施（A1，Raphael 已批 DQ-1 = A）。
- **测试漂移**（AUD-1）：M1 将由子代理修复；M2 / CR1-1 的 fixture 需要运行 writer 才能重新生成，留到调试阶段。
- **P4**：EventResult → Outcome 独立物化流程不需要（`317e6c8`）。将来若要，需先定义调用方与数据绑定。

## 8. 请 Codex 回复的格式

对每个 ID 给出：`决定（选项 / 升级 Raphael / 暂缓）`、一句理由、需要写入的 ADR 编号（新 ADR 从 0084 起）。Codex 的正式决定应写入 ADR / PROJECT_STATUS 并留下 commit（CLAUDE.md §0）；PM 据此签发实现任务。
