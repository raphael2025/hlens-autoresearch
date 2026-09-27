# ADR-0024: 历史可交易 universe（D-31）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-24，Codex 依 Raphael 授权批准）**；尚待实施（起草与修正：2026-09-24，批次 A1 起草；A1r、A1r2 按 Codex 两次独立复核退回意见修正） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权） |
| 批准者 | Codex（依 Raphael 2026-09-24 记录的授权） |
| 接受依据 | Codex 独立复核 A1r2 提交 `6d53cf5bdb3a13393a60f3a360ef048c91052864`：结论 **PASS**；独立重跑 docs 一致性 `7 passed`、全量 `1433 passed`、ruff check / format 与 mypy 无问题；于批次 A2 记录接受 |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（首次使用）；Phase 5+ 的策略研究依赖 |
| 影响范围 | Data / Contract / 研究完整性（Constitution C-L4） |
| 是否破坏兼容 | 否：`Instrument` 与 `Kind` 不变；新增模型为增量（见「契约影响」） |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md)（历史轴 / 知识轴与修订语义，本 ADR 依赖它）、[ADR-0022](0022-phase1-market-and-execution-scope.md) |

## 背景

Constitution C-L4 要求"标的池按当时可交易集合构建（含已下架标的）"。当前契约只有静态的 `Instrument`
（`core/domain/specs.py`：venue / symbol / instrument_type / base / quote），没有任何上市、暂停、下架的时间表达；
C1 复审把这一缺口登记为 D-31。

幸存者偏差的典型形态：用今天仍在交易的 symbol 列表回测过去；按最终成交量或最终存活筛选历史标的；
把 symbol 当作永久身份，把两个不同时期、同名的产品混成一个；下架后的收益缺失被静默当作 0 或被删行。

A1 版本用单一时刻 `t` 同时表达"历史上是否可交易"与"本机何时得知"，继承了 ADR-0023 A1 版本的缺陷。
本修订按 ADR-0023 把两条轴分开：**valid interval 按 `simulation_time` 判断，revision / vintage 按 `knowledge_cutoff` 判断**。
A1r2 进一步按 ADR-0023 §4 把本机追加顺序与修订优先级分开：listing revision 的选择由 `supersedes` 关系与来源证据决定，不由到达顺序决定。

## 裁决

### 1. 静态描述与可交易历史分离

- `Instrument` **继续**只表达静态描述，字段不变；**不**把上市 / 下架时间塞回静态身份。
- 新增独立、版本化的 **Instrument Listing / Availability 历史**，每条记录至少表达：
  venue、venue 原生稳定产品 ID（若数据源提供）、symbol、instrument type、base / quote、
  `tradable_from`、`tradable_until`（**右开区间**，可为空表示仍可交易）、状态与原因、source、revision，
  以及 ADR-0023 的 `available_time`（历史轴，由 availability policy 计算）、`ingest_time` 与 `knowledge_time`（知识轴）。
- 每次变化追加新 revision，不覆盖；revision 字段与 ADR-0023 §4 相同：`arrival_seq`（只表示本机追加顺序）、`revision_id`、
  `supersedes`、可用时的 `source_revision_id` 与 `source_revision_time`、payload hash；来源可证明的先后在 ingest 时持久化为 precedence 证据。

### 2. Listing episode、symbol 复用、暂停与改名（Codex 已确认）

- **symbol 不是永久身份**。venue 复用 symbol 时（同一 symbol 先下架、后又指向另一产品），必须产生两个不同的 listing episode。
- episode 键：优先使用 venue 原生稳定产品 ID；若 venue 不提供，退化为 `(venue, instrument_type, symbol, tradable_from)`，
  并在记录中**明确标注**这一退化。
- 上市、暂停、恢复、下架、symbol 改名都**追加**历史，不覆盖。
- **暂停 / 恢复**：暂停把当前可交易区间在暂停开始处关闭（`tradable_until` = 暂停开始）；恢复在**同一 episode** 内开启新的可交易区间。
- **改名**：有稳定产品 ID 时保留同一 episode，并记录 symbol 变化；没有稳定 ID 时按退化键形成新 episode，并记录与旧 episode 的关联。

### 3. 已下架资产不删除

已下架的标的永远保留在数据与 universe 历史中；不得因下架删除任何行、revision 或成员记录。

### 4. 双参数 universe 查询

在 `(simulation_time, knowledge_cutoff)` 下构建 universe：

1. 候选 listing revision 必须**同时**满足 `available_time <= simulation_time` 与 `knowledge_time <= knowledge_cutoff`；
2. 对每个 episode 按 ADR-0023 §5 的 **maximal-head 算法**选择：用 `knowledge_time <= knowledge_cutoff` 的 `supersedes` 边与 precedence 证据
   构建不可成环的 DAG，淘汰被其它候选直接或传递 supersede 的候选；只剩一个 head 则选它；**多个互不排序的 head 时 universe 构建
   fail closed** 并写质量事件，**不得**用 `arrival_seq`、墙钟或 payload hash 选一个；
3. 保留满足 `tradable_from <= simulation_time < tradable_until`（`tradable_until` 为空视为无穷）的 episode。

由此：来源的新 listing 修订先到、旧修订后到时仍选语义上的新修订；晚到本机的上市 / 下架修订在较早的 `knowledge_cutoff` 下不可见；来源公开时间晚于 `simulation_time` 的修订
不会进入更早的 simulation；较晚 cutoff 看到的新 revision 不改变旧 cutoff 下的结果。

### 5. 版本化的 `UniverseSelectionSpec`（Codex 裁决）

- 流动性、数据质量、产品类型等过滤是**版本化**的 `UniverseSelectionSpec`；规则变化 = 新版本。
- 过滤使用的指标必须**同时**满足历史可用（`available_time <= simulation_time`）与数据集知识截止（`knowledge_time <= knowledge_cutoff`）。
- **不得**按最终存活、最终成交量或今天的 symbol 列表筛选历史 universe。
- **Phase 1 不向 `Kind` 枚举增加取值**。`UniverseSelectionSpec` 是独立的新契约，在 `ResearchDatasetManifest`（ADR-0023 §6）中
  以 `name + SemVer + content hash` 绑定；**不能**用 `Ref` 引用，**不得**借用 `Kind.DATASET` 或其它既有 kind 冒充。
  若将来必须注册为可被 `Ref` 引用的对象，另起 ADR，并按消费者兼容性决定是否升 major。

### 6. Research Dataset 的绑定

`ResearchDatasetManifest`（ADR-0023 §6）必须绑定：`UniverseSelectionSpec` 的 `name + SemVer + content hash`、
listing 历史表的 `snapshot_id`、`simulation_time` 或区间、`knowledge_cutoff`，以及最终的**成员清单与排除原因清单**，
使 C-L4 可审计、可复现。

### 7. 退市后的缺失收益

退市后缺失的收益**不得**静默当作 0，也不得删行；按策略与 Outcome 规则显式处理，并写入质量记录或排除原因。
具体处理规则属于 Outcome / Strategy 规格（Phase 4 / 5），本 ADR 只规定"必须显式"。

## 明确不做

- 本批次不修改 `Instrument`、`Kind` 或任何契约与代码，不创建表。
- 不选择任何流动性阈值、成交量门槛或其它 `UniverseSelectionSpec` 数值。
- 不定义退市缺失收益的具体处理方法（Phase 4 / 5）。
- 不引入 Phase 1 第一切片之外的标的（ADR-0022）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** 静态 `Instrument` + 双轴 listing 历史 + 独立版本化 selection spec | 幸存者偏差可审计；symbol 复用有确定语义；与 ADR-0023 一致 | 多一张历史表与一个规格类型 | — |
| B 在 `Instrument` 上加 `listed_at` / `delisted_at` | 简单 | 混淆静态身份与时间历史；无法表达多次暂停、symbol 复用与修订；改动已发布契约 | 表达力不足 |
| C 用今天的交易所 symbol 列表 | 最省事 | 典型幸存者偏差 | 违反 C-L4 |
| D 只记录最新 listing 状态 | 表小 | 迟到修订会回写过去 | 违背 ADR-0023 |
| E 以 symbol 作永久键 | 直观 | symbol 复用时混淆两个产品 | 身份错误 |
| F 单一时刻 `t` 同时判断 valid interval 与 revision（A1 版本） | 简单 | 与 ADR-0023 A1 同一缺陷 | 已由 Codex 退回 |
| H 按到达顺序选最新 listing 修订（A1r 版本） | 简单 | 旧修订后到会覆盖新修订 | 已由 Codex 第二次复核退回 |
| G 为 `UniverseSelectionSpec` 新增 `Kind` 或借用 `Kind.DATASET` | 可用 `Ref` 引用 | 改变已发布枚举与 `REF_KEY_PATTERN`；借用 kind 是身份冒充 | Codex 裁决：manifest 内 `name + SemVer + hash` 绑定 |

## 契约、Schema 与迁移影响

- `Instrument` 字段**不变**；`Kind` 枚举**不变**；现有 38 个契约模型不变。
- 新增 listing / availability revision、`UniverseSelectionSpec`、成员与排除清单等**新模型**，属增量变化（minor）。
- 绑定通过 ADR-0023 的 `ResearchDatasetManifest` 完成；不给 `ReproducibilityTuple` / `DatasetRef` 增加必填字段，不升 major。
- listing 历史表的 Iceberg Schema 随表版本化。无既有数据需要迁移。

## 失败与恢复语义

- listing 数据源缺失或解析失败：该 `(simulation_time, knowledge_cutoff)` 的 universe 构建 fail closed（报告"不可构建"），
  不得回退为今天的 symbol 列表。
- 数据源不提供稳定产品 ID：使用退化键并标注；episode 边界存疑时写质量事件，不猜测合并。
- listing 修订缺少来源发布时间且 policy 无法证明：`available_time` 按 ADR-0023 保守取 `ingest_time`，并记录证据缺口。
- 同一 episode 出现 competing listing heads：该 `(simulation_time, knowledge_cutoff)` 的 universe 构建 fail closed，写质量事件；
  只能通过追加有证据的 precedence 记录解决，且只对不早于该记录 `knowledge_time` 的 cutoff 生效。
- `supersedes` 成环、自指或跨 episode：该 revision 拒绝写入并记录质量事件。
- 成员清单重建：给定相同 spec（`name + SemVer + hash`）、相同 snapshot、相同 `simulation_time` 与 `knowledge_cutoff`，结果按位一致。

## 安全边界

- 不涉及网络、凭据或交易。
- listing 历史与成员清单不可删除；更正只能追加新 revision。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 已下架标的在其可交易期内的 universe | 出现 |
| 2 | 同一 symbol 先下架、后指向另一产品 | 两个不同 episode，不混淆 |
| 3 | 暂停与恢复 | 暂停期间不在 universe 中；恢复后在同一 episode 重新出现；历史全部保留 |
| 4 | 有 / 无稳定产品 ID 的改名 | 前者同一 episode、记录 symbol 变化；后者新 episode 且关联旧 episode |
| 5 | valid-time × knowledge-time 交叉：同一 `simulation_time`、早晚两个 `knowledge_cutoff` | 早 cutoff 看不到晚到的上市 / 下架修订；晚 cutoff 看得到；早 cutoff 的结果不变 |
| 6 | valid-time × knowledge-time 交叉：同一 `knowledge_cutoff`、`simulation_time` 早于修订公开时间 | 修订不生效 |
| 7 | 历史 backfill：listing 的 `available_time < ingest_time`，有 policy 证据 | 在足够晚的 `knowledge_cutoff` 下，于历史 `simulation_time` 可见 |
| 8 | selection 使用未满足历史可用或知识截止的指标 | 拒绝或测试失败 |
| 9 | 相同 spec + snapshot + `simulation_time` + `knowledge_cutoff` 重建成员清单 | 按位一致 |
| 10 | Research Dataset manifest 缺少 spec 绑定、listing snapshot、cutoff 或成员 / 排除清单 | 不得进入实验 |
| 11 | 用今天的 symbol 列表或最终成交量构建历史池 | 不存在（静态检查与测试） |
| 12 | 退市后缺失收益被当作 0 或删行 | 不存在；显式记录 |
| 13 | `UniverseSelectionSpec` 以 `Ref` / `Kind.DATASET` 引用 | 不存在 |
| 14 | listing 修订乱序到达：来源新修订先 ingest、旧修订后 ingest | 仍选语义上的新修订 |
| 15 | 同一 episode 两个无 precedence 证据的不同 listing payload | universe 构建 fail closed，写质量事件 |
| 16 | listing 的 A → B → C supersede 链以任意顺序到达；dangling predecessor 后到 | 选 C；结果不因到达顺序反转 |
| 17 | 以 `arrival_seq` 决定 listing 修订优先级 | 不存在 |

## 后果

- 正面：Constitution C-L4 有可执行、可审计的数据语义；历史 backfill 的 listing 数据能正确用于历史 simulation；
  symbol 复用与迟到修订不再造成身份混淆或 look-ahead。
- 负面 / 代价：每份 Research Dataset 多一份成员清单；listing 数据源的质量与 availability policy 证据直接限制 universe 的可构建性。
- 对复现性：universe 由 spec + snapshot + `simulation_time` + `knowledge_cutoff` 唯一决定，可按位重建。

## 开放义务

- 实施前确认 Binance 公开数据中可用的 listing / 状态信息来源、其是否提供稳定产品 ID 与修订发布时间；不足之处按退化键
  与保守 `available_time` 处理并记录。
- 退市缺失收益的处理规则在 Phase 4 / 5 的 Outcome 与 Strategy 规格中定义。
- 若将来需要以 `Ref` 引用 `UniverseSelectionSpec`，另起 ADR。

## 版本策略

- 本 ADR 不改变 `CONTRACT_SCHEMA_VERSION`，不改变 `Kind`。新增模型为 minor。
- `UniverseSelectionSpec` 自身按 SemVer 版本化并带内容哈希；规则变化发布新版本，旧版本保留以复现旧实验。

## 合规检查（A2 接受时）

- [x] 不修改 `Instrument`、`Kind` 或任何契约、代码
- [x] 不选择任何数值阈值
- [x] 不修改 Constitution 原则；把 C-L4 落为可审计的数据义务
- [x] 接受时问题已由 Codex 裁决并写入（不新增 `Kind`；manifest 绑定；暂停 / 改名语义确认）
- [x] listing revision 选择使用 maximal-head 算法，competing heads fail closed（A1r2）
- [x] Codex 独立复核 A1r2（`6d53cf5`，PASS）并于 2026-09-24 接受（A2 记录）
- [x] universe 绑定写入 `03-data.md` §3 / §7（`binance.spot.btc-eth@1.0.0` + content hash）—— A2 执行
- [ ] 验收矩阵 1 ~ 17 —— 实施批次验证
