# ADR-0007: 三层验证架构与两步冻结（D-09 结构部分）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（等待 Raphael 批准本 ADR） |
| 日期 | 2026-09-23 |
| 决策者 | Raphael（H-1、H-2 已于 2026-09-23 接受；本 ADR 的实施细节待批准） |
| 起草者 | Claude Code |
| 相关 Phase | Phase 0（结构冻结）、Phase 4（参数校准与冻结） |
| 影响范围 | Research Constitution / Validation / Contract / 复现性 |
| 是否破坏兼容 | 是：改变 Constitution 的组织方式，并向复现元组增加字段 |

## 背景

D-09 提案（`docs/research/proposals/d09-validation-threshold-proposal.md`）指出：把 OOS 长度、最小样本量、基准、参数稳定性、成本压力这五类门槛写成单一固定数字会造成错误筛选。Raphael 于 2026-09-23 接受 H-1（三层结构）与 H-2（两步冻结）。

本 ADR 把这两项决定落成架构；**不选择任何数值**。

## 决策

### 1. 三层结构

| 层 | 内容 | 可变性 | 存放位置 |
|---|---|---|---|
| **1. Research Constitution** | 不可变的研究原则：必须有哪些门槛、如何使用、如何计数、如何裁决 | 只能通过 ADR 修改；**永远不能因为实验结果而修改**；只前向生效 | `docs/research/constitution.md` |
| **2. Validation Profile** | 按"标的 × 周期 × 研究类别"给出的阈值与验证参数（OOS 长度、最小有效样本量、基准设定、参数稳定性、成本压力等） | 版本化；**一经被实验使用即不可变**；新值 = 新版本，只对之后登记的实验生效 | 版本化 Profile 定义（Phase 0 定契约，Control Plane 登记） |
| **3. Experiment Metadata** | 每个实验实际使用的 Profile 版本与实验特有配置 | 追加式、不可修改 | ExperimentRun 记录（`docs/architecture/06-experiment.md`） |

### 2. 归属规则（语义边界）

- **原则**属于 Constitution。
- **可校准的阈值**属于 Validation Profile。
- **单次实验的设置**属于 Experiment Metadata。
- **不得**把临时性的数值阈值写进 Constitution。

判定归属的问题："这个东西会因为数据、成本模型或校准结果而改变吗？"
会 → Profile；不会 → Constitution；只对这一次实验成立 → Experiment Metadata。

| 例子 | 层 |
|---|---|
| "必须使用封存样本外区间，且每个假设族只开封一次" | Constitution |
| "封存样本外 = 12 个月" | Profile |
| "本实验使用 `vp:btcusdt-1h-swing@1.0.0`，开封于某日" | Experiment Metadata |
| "显著性必须经多重检验校正，尝试次数取假设族累计值" | Constitution |
| "DSR 阈值 = 0.95" | Profile |
| "本实验族累计尝试 137 次" | Experiment Metadata |

### 3. 两步冻结

| 步骤 | 时点 | 冻结什么 |
|---|---|---|
| **Step 1** | Phase 0 | Constitution 原则 + 验证架构（三层结构、Profile 的字段与选择规则、Experiment Metadata 字段） |
| **Step 2** | Phase 4 校准完成后、Phase 5 之前 | 初始 Validation Profile 的参数值（依据真实数据特征、Outcome 定义、成本模型与实证校准） |

规则：
1. Phase 4 的校准**不是**修改 Constitution 的许可。
2. 实验结果**不得**追溯修改该实验所使用的 Profile。
3. 实验永久保留它使用的 Profile 版本，用于历史复现。
4. 校准后的参数版本化且不可变；后续变化只能发布新版本。
5. Profile 由确定性映射规则选定（标的、周期、预登记的研究类别），研究者不能自选；不得把实验重新归类到更宽松的 Profile。

### 4. 接受本 ADR 后需要的修改

以下修改在批准后执行，**现在不做**：

| 目标 | 修改 |
|---|---|
| `docs/research/constitution.md` | 重组为纯原则；移出 TBD-1 ~ TBD-5（改为"由 Profile 定义"）；加入 Profile 绑定与选择规则；版本升到 1.0.0 时需 Raphael 批准 |
| `docs/architecture/06-experiment.md` | 复现元组增加 `validation_profile_version`（与既有 `constitution_version` 并列）——属于冻结内容的变更，由本 ADR 授权 |
| `core/contracts/` | 新增 `ValidationProfile` 契约与 `ProfileSelectionRule`；`ValidationReport` 绑定 Profile 版本 |
| `docs/architecture/07-validation.md` | 各验证门的阈值来源改为引用 Profile |
| Phase 4 交付物 | 增加校准报告与初始 Profile 版本 |

### 5. 不在本 ADR 范围内

- 五类门槛的**具体数值**（D-09 提案 §C 仍是建议，未批准）
- H-3 ~ H-7（封存边界与轮换、目标假阳性率、现货 / 永续、研究类别划分等）
- ADR-0005 / ADR-0006 的 Q-1 ~ Q-7
- Profile 与劣化监控阈值的关系（ADR-0006 Q-6）

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 把数值直接写进 Constitution | 数值会随数据、成本、校准变化；写进不可变文件会导致要么僵化、要么被迫频繁修改宪法 |
| 只有 Constitution + 实验记录，无 Profile | 没有版本化的中间层，阈值变化无法审计，也无法按标的 / 周期区分 |
| Profile 可由研究者自选 | 会出现"挑选最宽松 Profile"的行为 |

## 后果

- 正面：原则稳定、参数可校准、历史实验可复现；阈值变化有版本与审计。
- 负面：多一层契约与注册表需要实现（Phase 0 契约 + Phase 4 校准）。
- 对复现性：实验同时记录 Constitution 版本与 Profile 版本，旧实验永远可按原规则重放。
