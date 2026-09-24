# ADR-0017: Provider 接口的交付节奏（D-24，方案 B）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24 起草，等待 Codex 文档复核；未获批准，不得实施） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（定节奏）；各 Provider 落地在其首次被消费的 Phase |
| 影响范围 | Plugin / Contract / Phase 验收范围 |
| 是否破坏兼容 | 否（不改变任何现有契约字段；见 §5） |
| 前置 | [ADR-0002](0002-architecture-baseline.md)、[ADR-0005](0005-research-production-boundary.md) |

## 背景

`05-plugin.md` §3 的表头写着"冻结语义，**签名在 Phase 0 以代码形式定义**"，列出十类
Provider；Phase 0 的 roadmap 验收标准里却没有"Provider 接口已定义"这一条，
`core/contracts/README.md` 也记着"Provider 签名交付范围仍需明确"。
仓库中 `core/contracts/` 目前只有验证架构契约与 Schema 导出，没有任何 Provider Protocol。

这是一处**文档与验收范围的不一致**，必须决定交付节奏，否则每次 Phase 0 复审都会重新争论
"Provider 接口算不算 Phase 0 的欠账"。可选的两条路：

- **方案 A**：在 Phase 0 就写出十类 Provider 的可执行 Protocol。
- **方案 B**：Phase 0 只冻结职责与语义，可执行接口随首次消费它的 Phase 交付。

方案 A 的代价是具体的：十套没有任何消费者、没有任何实现、没有真实数据形态约束的
Protocol，其输入输出类型只能靠猜。等到 Phase 1 真正写第一个 FeatureProvider 时，
这些签名几乎必然要改——而那时它们已经是"冻结契约"，每次修改都要走 ADR。
这与 P1（Freeze Contracts, Evolve Implementations）的本意相反：应该冻结的是**已经理解的东西**。

## 精确决定

**选择方案 B。**

### 1. Phase 0 冻结什么

Phase 0 只冻结 `05-plugin.md` 已有的**概念层内容**，不新增代码：

| 冻结项 | 位置 |
|---|---|
| 十类 Provider 的**职责** | `05-plugin.md` §3 第 2 列 |
| **概念输入输出** | `05-plugin.md` §3 第 3 列 |
| **确定性语义** | `05-plugin.md` §1、§3 第 4 列（`deterministic` 必须声明；非确定性插件必须记录全部输入输出） |
| **版本语义** | 插件版本化、进入复现元组、`name@version` 键格式（`06-experiment.md` §2、ADR-0010 §D-16） |
| **审计语义** | 插件不得写 Control Plane；不得访问网络（声明的除外）；`content_hash` 进入 `plugin_versions` |

**Phase 0 不创建十套无消费者的 Python Protocol。**

### 2. 每类 Provider 的交付义务

**每一类 Provider 必须在首次消费它的 Phase 开始实现之前**完成下列三项，并在**该 Phase 的验收**中检查：

1. **可执行 Protocol**：该类 Provider 的 Python 接口定义，位于 `core/contracts/`；
2. **DTO**：该接口的输入输出数据契约（Pydantic + JSON Schema 导出，带 `schema_version`）；
3. **provider-agnostic contract tests**：不依赖任何具体实现的一致性测试——
   任何声称实现该接口的插件都必须通过同一组测试（与 `06-experiment.md` §6 第 5 条对
   BacktestProvider 的既有要求同源）。

"开始实现之前"是硬顺序：接口与契约测试先于第一个实现，而不是从第一个实现里反推接口。

### 3. 首次消费 Phase（依 roadmap 现状）

| Provider | 首次被消费 |
|---|---|
| FeatureProvider | Phase 1 |
| StateProvider | Phase 2 |
| EventProvider | Phase 3 |
| OutcomeProvider | Phase 4 |
| KnowledgeProvider | Phase 0.5 |
| StrategyProvider、RiskProvider、BacktestProvider | Phase 5 |
| LLMProvider | Phase 7 |
| SyntheticMarketProvider | Phase 9 |

本表**依据 roadmap 现状列出**，用于说明节奏，不改变 roadmap 的任何内容；
roadmap 若调整 Phase 归属，本表随之解释，无需修改本 ADR。
基础设施 Adapter（`CollectorAdapter`、`StorageAdapter`、`CatalogAdapter`、
`ComputeEngineAdapter`、`EventBusAdapter`）不是研究插件，适用同一交付节奏，
其时点取决于 D-01、D-02、D-10。

### 4. 实施时必须同步的文档

**未来某个 Provider 落地时**，必须在同一批次内同步修订（本轮**不做**）：

| 目标 | 修订内容 |
|---|---|
| `05-plugin.md` §3 | 表头"签名在 Phase 0 以代码形式定义"改为按本 ADR 的交付节奏表述 |
| `02-domain.md` §4 | `core/contracts/` 的内容说明补入该 Provider 接口 |
| `core/contracts/README.md` | 删除"Provider 签名交付范围仍需明确"，写明当前已交付的接口 |
| 相关 Phase 的验收标准 | 把该 Provider 的 Protocol + DTO + contract tests 列为验收项 |

roadmap 的 Phase 验收标准属于 Codex / Raphael 的决定范围；本 ADR 只记录"获批后需要同步"，
不代替任何人修改 roadmap。

## 明确不做

- **本轮不写任何 Provider Protocol、DTO 或 contract test。**
- **本轮不直接修改 `05-plugin.md`、`02-domain.md`、`core/contracts/README.md` 或 roadmap**——
  这些是本 ADR **获批后**随实施批次执行的同步义务，不是起草阶段的动作。
- 不改变十类 Provider 的划分、职责或确定性声明。
- 不决定 Provider 的发现机制（`05-plugin.md` §5 的 entry points 保持现状）。
- 不决定研究与生产是否共用同一个 Provider 实现（ADR-0005 Q-2 仍开放）。
- 不扩大或缩小任何 Phase 的范围；不开启任何 Phase。

## 运行时延期义务

| 义务 | 说明 |
|---|---|
| 接口兼容性检查 | `contract_version` 是否兼容、manifest 是否合法，属未来 Plugin Registry |
| 一致性测试执行 | 某个具体插件是否真的通过 contract tests，属该 Phase 的 CI 与验收 |
| 沙箱隔离 | Phase 7+ 生成代码的沙箱运行（`09-security.md` §4） |
| 插件内容哈希 | `plugin_versions` 中的 `content_hash` 由打包器计算并由 Registry 核验 |

## Schema 与迁移影响

- **不改变任何现有契约字段，不新增契约模型，不改变 `CONTRACT_MODELS`。**
- 不需要重新导出 `schemas/`；`schemas/v1/` 与 `tests/vectors/v1/` 不受影响。
- `CONTRACT_SCHEMA_VERSION` 不变（仍为未发布的 `2.0.0`）。
- 本 ADR 的效果是**交付节奏与验收范围**，不是数据结构变化。

### 与 D-25 的关系

D-25 关于"收窄仍属未发布的 `2.0.0`"的结论适用于 ADR-0011 ~ ADR-0016；
本 ADR 不改变契约内容，因此不涉及版本号问题。

## 验收测试矩阵

> 本 ADR 的验收对象是**流程与文档一致性**，不是运行时行为。
> 下列检查在本 ADR 获批后、以及每个 Provider 落地时执行；本轮只起草，未执行。

| # | 场景 | 期望 |
|---|---|---|
| 1 | Phase 0 结束时 `core/contracts/` 中的 Provider Protocol 数量 | 0（方案 B 的直接结论） |
| 2 | `05-plugin.md` §3 的表头表述与本 ADR | 获批后一致（当前不一致，是本 ADR 要解决的问题） |
| 3 | `core/contracts/README.md` 的"交付范围仍需明确" | 获批后按 §4 处理 |
| 4 | 某 Provider 的首个实现提交时，其 Protocol / DTO / contract tests | 三者已先行存在，且在该 Phase 验收项中 |
| 5 | 任一 Phase 的 Provider 实现早于其接口与契约测试 | 该 Phase 验收不通过 |
| 6 | 本轮改动是否触及 `05-plugin.md` / `02-domain.md` / roadmap / `core/contracts/README.md` | 未触及 |
| 7 | 本轮改动是否新增 Python 代码或 Schema | 未新增 |

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **B（本 ADR）** Phase 0 冻结语义，接口随首次消费的 Phase 交付 | 接口在有真实消费者时定义，签名有依据；Phase 0 不背无法验证的欠账 | Phase 0 结束时 `core/contracts/` 没有 Provider 接口，需要在文档中说清这是决定而非遗漏 | — |
| A 在 Phase 0 写出十套 Protocol | 文档表头无需修改；"接口先行"表面上更齐整 | 十套无消费者、无实现、无数据形态约束的签名几乎必然在 Phase 1–9 被推翻，而它们已是冻结契约，每次修改都要走 ADR | 冻结未被理解的东西，代价高于收益 |
| C 不作决定，继续并存 | 零成本 | `05-plugin.md`、roadmap 与 `core/contracts/README.md` 的不一致长期存在，每次复审重新争论 | 文档矛盾必须收敛 |

## 后果

- 正面：`05-plugin.md` 与 roadmap 验收表之间的矛盾有了明确答案；
  Phase 0 的交付边界清晰——没有 Provider 接口是**决定**，不是欠账；
  每类接口在有真实消费者时定义，并且接口与 provider-agnostic 测试先于实现。
- 负面 / 代价：Phase 0 关闭时 `core/contracts/` 中没有 Provider 接口，
  任何后续复审都必须读到本 ADR 才知道这是有意为之——因此 §4 的文档同步义务不可省略；
  每个 Phase 的工作量增加一块（接口 + DTO + 契约测试）。
- 对复现性的影响：无。本 ADR 不改变任何数据结构或哈希。

## 合规检查

- [ ] 不修改 `05-plugin.md`、`02-domain.md`、`core/contracts/README.md`、roadmap（获批后随实施同步）
- [ ] 不扩大 Phase 范围，不开启任何 Phase
- [ ] 不修改任何已批准 ADR 的正文
- [ ] 不新增代码、测试或 Schema
- [ ] 不代替 Codex / Raphael 修改 Phase 验收标准
