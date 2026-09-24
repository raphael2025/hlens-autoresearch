# ADR-0024: 历史可交易 universe（D-31）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24，批次 A1；待 Codex 复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（首次使用）；Phase 5+ 的策略研究依赖 |
| 影响范围 | Data / Contract / 研究完整性（Constitution C-L4） |
| 是否破坏兼容 | 否：`Instrument` 字段不变；新增模型为增量（见「契约影响」） |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md)（双时间与修订语义，本 ADR 依赖它）、[ADR-0022](0022-phase1-market-and-execution-scope.md) |

## 背景

Constitution C-L4 要求"标的池按当时可交易集合构建（含已下架标的）"。当前契约只有静态的 `Instrument`
（`core/domain/specs.py`：venue / symbol / instrument_type / base / quote），没有任何上市、暂停、下架的时间表达；
C1 复审把这一缺口登记为 D-31。

幸存者偏差的典型形态包括：用今天仍在交易的 symbol 列表回测过去；按最终成交量或最终存活筛选历史标的；
把 symbol 当作永久身份，把两个不同时期、同名的产品混成一个；下架后的收益缺失被静默当作 0 或被删行。
这些都会让历史结果看起来比真实可得的更好。

Phase 1 第一切片只有两个 spot 标的（ADR-0022），但 universe 语义必须在 Collector 大规模实现之前确定，
否则后续数据无法补救。

## 裁决

### 1. 静态描述与可交易历史分离

- `Instrument` **继续**只表达静态描述，字段不变；**不**把上市 / 下架时间塞回静态身份。
- 新增独立、版本化的 **Instrument Listing / Availability 历史**，每条记录至少表达：
  venue、venue 原生稳定产品 ID（若数据源提供）、symbol、instrument type、base / quote、
  `tradable_from`、`tradable_until`（**右开区间**，可为空表示仍可交易）、状态与原因、source、revision、`available_time`。
- 该历史采用 ADR-0023 的双时间与 append-only revision 语义：每次变化追加新 revision，带 `revision_seq`、`supersedes`、
  payload hash、`ingest_time` 与 `available_time`。

### 2. Listing episode 与 symbol 复用

- **symbol 不是永久身份**。venue 复用 symbol 时（同一 symbol 先下架、后又指向另一产品），必须产生两个不同的 listing episode。
- episode 键：优先使用 venue 原生稳定产品 ID；若 venue 不提供，退化为 `(venue, instrument_type, symbol, tradable_from)`，
  并在记录中**明确标注**这一退化。
- 上市、暂停、恢复、下架、symbol 改名都**追加**历史，不覆盖。
- 本 ADR 对暂停与改名的具体化（供 Codex 复核确认）：暂停把当前可交易区间在暂停开始处关闭，恢复在同一 episode 内开启新的
  可交易区间；有稳定产品 ID 时改名保留同一 episode 并记录 symbol 变化，没有稳定 ID 时改名按退化键形成新 episode 并记录关联。

### 3. 已下架资产不删除

已下架的标的永远保留在数据与 universe 历史中；不得因下架删除任何行、revision 或成员记录。

### 4. 双时间 universe as-of 算法

在模拟时刻 `t` 构建 universe：

1. 只使用 `available_time <= t` 的 listing revision；
2. 对每个 episode 按 ADR-0023 选择当时已知的最新 revision；
3. 保留满足 `tradable_from <= t < tradable_until`（`tradable_until` 为空视为无穷）的 episode。

**晚到**的上市 / 下架修订**不能**回写过去的知识：在它的 `available_time` 之前构建的 universe 不受其影响。

### 5. 版本化的 UniverseSelectionSpec

- 流动性、数据质量、产品类型等过滤是**版本化**的 `UniverseSelectionSpec`；规则变化 = 新版本。
- 过滤只能使用在 `t` 时**已可用**（`available_time <= t`）的指标。
- **不得**按最终存活、最终成交量或今天的 symbol 列表筛选历史 universe。

### 6. Research Dataset 的绑定

每份 Research Dataset 必须绑定：`UniverseSelectionSpec` 版本、listing 历史表的 `snapshot_id`，以及最终的
**成员清单与排除原因清单**，使 C-L4 可审计、可复现。该绑定与 ADR-0023 §5 的 PIT spec 绑定属于同一 manifest。

### 7. 退市后的缺失收益

退市后缺失的收益**不得**静默当作 0，也不得删行；按策略与 Outcome 规则显式处理，并写入质量记录或排除原因。
具体处理规则属于 Outcome / Strategy 规格（Phase 4 / 5），本 ADR 只规定"必须显式"。

## 明确不做

- 本批次不修改 `Instrument` 或任何契约与代码，不创建表。
- 不选择任何流动性阈值、成交量门槛或其它 `UniverseSelectionSpec` 数值。
- 不定义退市缺失收益的具体处理方法（Phase 4 / 5）。
- 不引入 Phase 1 第一切片之外的标的（ADR-0022）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** 静态 `Instrument` + 独立双时间 listing 历史 + 版本化 selection spec | 幸存者偏差可审计；symbol 复用有确定语义；与 ADR-0023 一致 | 多一张历史表与一个规格类型 | — |
| B 在 `Instrument` 上加 `listed_at` / `delisted_at` | 简单 | 混淆静态身份与时间历史；无法表达多次暂停、symbol 复用与修订 | 表达力不足且改动已发布契约 |
| C 用今天的交易所 symbol 列表 | 最省事 | 典型幸存者偏差 | 违反 C-L4 |
| D 只记录最终状态（最新 listing） | 表小 | 迟到修订会回写过去 | 违背 ADR-0023 |
| E 以 symbol 作永久键 | 直观 | symbol 复用时混淆两个产品 | 身份错误 |

## 契约、Schema 与迁移影响

- `Instrument` 字段**不变**；现有 38 个契约模型不变。
- 需要新增的领域契约（listing / availability revision、`UniverseSelectionSpec`、universe 成员与排除清单）作为**新模型**加入，
  属增量变化（minor）。
- **待 Codex 在接受时裁定**：`UniverseSelectionSpec` 是否作为可被 `Ref` 引用的版本化规格而新增 `Kind` 取值。
  向 `Kind` 增加取值会改变 `REF_KEY_PATTERN` 与导出 Schema 中的枚举；02-domain.md §3 只把"添加可选字段"明确列为 minor，
  枚举扩展的归类需要在接受时明确。
- Research Dataset 绑定的契约强制问题与 ADR-0023 的 manifest 裁定一并决定（必填字段变更 = major，D-25）。
- listing 历史表的 Iceberg Schema 随表版本化。无既有数据需要迁移。

## 失败与恢复语义

- listing 数据源缺失或解析失败：该时点的 universe 构建 fail closed（报告"不可构建"），不得回退为今天的 symbol 列表。
- 数据源不提供稳定产品 ID：使用退化键并在记录中标注；episode 边界存疑时写质量事件，不猜测合并。
- 迟到的下架修订：按 ADR-0023 追加，只影响其 `available_time` 之后的 universe。
- 成员清单重建：给定相同 spec 版本、相同 snapshot 与相同 `t`，结果按位一致。

## 安全边界

- 不涉及网络、凭据或交易。
- listing 历史与成员清单不可删除；更正只能追加新 revision。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 已下架标的在其可交易期内的 as-of universe | 出现 |
| 2 | 同一 symbol 先下架、后指向另一产品 | 两个不同 episode，不混淆 |
| 3 | 迟到的 listing / delisting 修订 | 不改变其 `available_time` 之前的 universe |
| 4 | 暂停与恢复 | 暂停期间不在 universe 中；恢复后重新出现；历史全部保留 |
| 5 | 未来的 universe 状态 | 不污染过去的 as-of 结果 |
| 6 | selection rule 使用 `t` 之后才可用的指标 | 拒绝或测试失败 |
| 7 | 相同 spec 版本 + snapshot + `t` 重建成员清单 | 按位一致 |
| 8 | Research Dataset 缺少 spec 版本、listing snapshot 或成员 / 排除清单 | 不得进入实验 |
| 9 | 用今天的 symbol 列表或最终成交量构建历史池 | 不存在（静态检查与测试） |
| 10 | 退市后缺失收益被当作 0 或删行 | 不存在；显式记录 |
| 11 | 无稳定 ID 时的退化键 | 使用 `(venue, instrument_type, symbol, tradable_from)` 且被标注 |

## 后果

- 正面：Constitution C-L4 第一次有可执行、可审计的数据语义；symbol 复用与迟到修订不再造成身份混淆或 look-ahead。
- 负面 / 代价：每份 Research Dataset 多一份成员清单；listing 数据源的质量直接限制 universe 的可构建性。
- 对复现性：universe 由 spec 版本 + snapshot + `t` 唯一决定，可按位重建。

## 开放义务

- 接受时由 Codex 裁定：`UniverseSelectionSpec` 是否新增 `Kind` 取值及其版本归类；manifest 绑定的契约强制方式（与 ADR-0023 一并）。
- 接受时确认 §2 对暂停与改名的具体化。
- 实施前确认 Binance 公开数据中可用的 listing / 状态信息来源与其是否提供稳定产品 ID；不足之处按退化键处理并记录。
- 退市缺失收益的处理规则在 Phase 4 / 5 的 Outcome 与 Strategy 规格中定义。

## 版本策略

- 本 ADR 不改变 `CONTRACT_SCHEMA_VERSION`。新增模型为 minor；对既有模型的必填字段变更为 major（D-25）。
- `UniverseSelectionSpec` 自身按 SemVer 版本化；规则变化发布新版本，旧版本保留以复现旧实验。

## 合规检查（Proposed 阶段）

- [x] 不修改 `Instrument` 或任何契约、代码
- [x] 不选择任何数值阈值
- [x] 不修改 Constitution 原则；把 C-L4 落为可审计的数据义务
- [ ] Codex 复核并接受 —— 待进行
- [ ] `Kind` 扩展与 manifest 强制方式的裁定 —— 接受时
