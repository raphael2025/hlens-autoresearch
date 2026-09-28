# ADR-0079: Paper deviation 与 P8 声明范围绑定

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Codex，依 Raphael 2026-09-28 授权决定 |
| 起草者 | Codex-TechLead（W1-P10） |
| 相关 Phase | Phase 10，依赖 Phase 8 |
| 影响范围 | `research/router/` paper deviation 报告及其读取兼容 |
| 是否破坏兼容 | 新报告 schema major 升级；旧报告保留历史读取 |

## 背景

ADR-0043 第 4 条和 roadmap Phase 10 要求纸面运行与回测的偏差处于 P8 给出的声明范围内。当前 `PaperDeviation` 只绑定 Router run、参照回测与比较标的，无法证明它使用了哪个 P8 范围。P8 的范围身份来自验证所用 `ValidationProfile.scope`（`venue`、`symbol`、`timeframe`、`research_class`）；`ValidationReport` 绑定 Profile ref 与内容哈希，并以 `subject` 标识被验证的策略。P8 `retro_audit` 是另一种历史报告，只有被审计 subject 清单，不定义 Router deviation 的市场范围，因此不作为范围来源。

本决定不修改任何偏差统计、阈值、验证门、Profile 冻结登记或冻结契约。P10-FREEZE 继续生效：Router 的研究层证据模式及本范围绑定均不读取 `ProfileFreezeRegistry`，也不证明 Promotion 或生产资格。

## 决策

1. 创建 scope-bound deviation 时，调用方必须传入 Router 自身的 P8 `ValidationReport` 和该报告引用的精确 `ValidationProfile`。缺任一输入即 `DeviationError`。
2. 绑定前 fail closed 校验：报告 subject 必须精确等于 Router 策略 ref；报告的 Profile ref 与 `validation_profile_hash` 必须匹配传入 Profile；Profile 内容哈希重新计算；报告 verdict 必须是 `PASS` 且至少含一项 `G5.*` 密封 OOS 门。该检查只确认范围声明的来源和资格完整性，不重跑验证、不推断任何 deviation 结论。
3. 本范围身份由 `ValidationProfile.scope` 的四个字段、Profile ref / 内容哈希、P8 report 内容哈希及 `scope_schema_version` 组成，并以 `scope_hash = SHA-256(canonical content)` 绑定进 deviation payload。范围版本当前精确支持 `1.0.0`；不认识或不支持的范围版本拒绝读取 / 校验，不按近似版本降级。
4. Router run 的定价标的集合必须恰好为单个 `ProfileScope.symbol`；参照的成交标的和显式 `reference_request` 的定价标的仍按现有规则核对。价格 bar 不携带 venue / timeframe 元数据，因此报告绑定这些 Profile 声明身份，但不声称从 bar 载荷独立验证 venue / timeframe。无法证明的事实保持为声明，不作推断。
5. 新 `paper_deviation` payload 使用 schema `2.0.0`，包含 `declared_scope` 与其 `scope_hash`；外层 `deviation_hash` 覆盖完整 payload。偏差输出仍然只是描述性统计，无阈值、无 PASS / FAIL 结论。
6. 历史 schema `1.0.0` 报告继续由现有通用报告存储按原哈希读取和展示，字段与哈希不迁移、不重写；它们标记为未绑定范围的 legacy 描述材料，不得当作满足本 ADR / ADR-0043 声明范围要求的证据。任何需要范围合规的消费者必须要求 schema `2.0.0`、验证 `deviation_hash`、验证嵌套 `scope_hash` 并执行范围一致性校验；缺失或不匹配一律 fail closed。schema 2.x 不自动接受 1.x；后续范围语义或兼容升级另立 ADR。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 只复制 Profile.scope 字段 | 接口轻 | 无 Profile 内容身份及 P8 验证报告来源，调用者可拼造范围 | 无法证明是 P8 声明范围 |
| 绑定完整 Profile 与 P8 ValidationReport | 同时绑定声明内容、Profile 身份和 Router 验证主体；无需修改 P8 写入端 | 调用方须显式提供两个现有对象；bar 无法自证 venue / timeframe | **选用**；明确保留声明边界，不虚构数据源证据 |
| 修改 P8 writer 新增专用 scope DTO | 可提供专用范围载荷 | 跨模块改动且重复 Profile 已有范围字段；对本任务不是必要条件 | 不采用 |

## 后果

- 正面：每个新 deviation 内容哈希均绑定精确 P8 报告与 Profile 范围；缺失、错主体、错 Profile、未 PASS、无 G5 或标的不匹配均 fail closed。
- 负面 / 代价：旧报告无法追溯补全范围；venue 与 timeframe 的实际 bar 对齐仍依赖上游声明，当前 `PriceBar` 不携带这些维度。
- 迁移：没有旧报告重写。新写入必须提供验证报告和 Profile；旧文件继续原样可读，但不满足声明范围证据要求。
- 复现性：新范围字段与 report hash 纳入 `deviation_hash`，相同输入生成相同身份；旧 1.0.0 身份维持不变。

## 合规检查

- 未改变冻结契约、Constitution、Validation Profile 数值或验证阈值。
- 未读取 Profile 冻结登记；P10-FREEZE 与 ADR-0043 B62 保持有效。
- 未修改 P8 写入端、`core/`、P13 或执行能力。

## 参考

- [ADR-0043](0043-dynamic-strategy-router.md) 第 4 条及 B62
- [ADR-0046](0046-simulated-execution-service.md)（仅边界参考）
- [roadmap Phase 10](../research/roadmap.md)
- [Phase 10 范围复核](../plans/2026-09-28-module-foundation-completion.md) 的 DP-P10-SCOPE
