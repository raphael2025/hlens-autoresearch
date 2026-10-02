# 模块底层代码完成计划（当前模块视图，2026-10-01）

> 当前执行清单以[剩余底层代码完成计划](2026-09-28-remaining-code-gaps.md)为唯一来源；本文件提供模块映射、候选分支状态、依赖、文件边界、并行限制与代码完成条件。2026-09-28 的原计划与任务板保留作历史记录，不作为当前状态依据。

## 当前基线

- **2026-10-02 第三轮**：ADR-0101 ~ 0106、0108 ~ 0110 的实现全部整合到 `phase/1`（模块表与状态见[剩余代码计划](2026-09-28-remaining-code-gaps.md)「本轮模块」）；无剩余 `CODE_GAP`。2026-10-01 的分支轮候选已核对、归档（`refs/archive/2026-10-02/`）并删除。下一步：门禁 → 合入 `main` → E1-CAP-1 重跑。下表各行已按本轮更新。
- **2026-10-01 更新**：四项任务卡（E1-ARCHIVE-REUSE、E1-RAW-WINDOW-REUSE、E1-V2-REPLAY、P7-CS-EXEC）均已收口（`fe842b3`、`63d09a4`），全仓门禁全绿（pytest 9313 passed / 144 skipped / 0 failed；ruff / format / mypy 通过），随 PR #20 整合入 `main`；见 [W1 门禁修复记录](../reviews/2026-10-01-w1-gate-repair.md)。下列较早基线描述保留作历史；E1-CAP-1 与 Phase 验收仍开放。
- 代码权威基线为 `main@b5f80fe`，且 `origin/main` 同步；PR #17–#19 均已合入，GitHub 当前无开放 PR，PR #19 为 `MERGED`、非 Draft。主线最新 HEAD 未运行全仓门禁或 Phase 验收。
- 本地快照为 13 个分支 / 13 个 worktree；当前根 worktree 在 `phase/1@4c2356c`，有计划 / 状态文档改动；4 个 Claude feature worktree 有未提交文件，详见下表。计数为 2026-10-01 快照。
- `phase/1@47446f4` 的旧选择性 Phase 1 infrastructure suite 曾有 `11 failed, 2287 passed, 88 skipped`；失败已逐项分诊，当前完整选择集复跑为 `2298 passed, 88 skipped in 855.34s`。这是候选分支结果，不代表 `main` 全仓门禁或 Phase 验收。
- DQ-10 public replay / v2 golden compatibility 定向集 `35 passed`；这是候选分支结果，仍待独立审阅 / 整合。
- 受影响策略回归 `257 passed, 1 skipped, 1 failed`；唯一旧 B67 dataset-report hash 失败已在改动前的 `47446f4` 上精确复现，保留为已有失败证据，不变更 golden。
- P7 横截面编译、binding、lowering 和 feature provider 专项 `105 passed`；该子集不构成完整 P7 候选审查或 Phase 验收。
- E1-CAP-1（32 MiB）与 DQ-9 均未完成 / 未定。main 正式探针在 100k `verify_archive` stage 后中断；候选 full-shape probe 的 100k `verify_archive` 为 78.207 秒，500k 同 stage 超过 15 分钟、约读 17.7 GB 后中断。候选诊断揭示逐窗口 Raw 重扫是显著成本候选，但没有总容量 PASS / FAIL 结论；局部结果不等于 W1 门禁或 Phase 验收。

## 模块状态总览

| Phase / 模块 | `main@b5f80fe` 状态 | 未合入工作 / 缺口 | 依赖与边界 |
|---|---|---|---|
| P0.5 Knowledge | 实现已在主线，未验收 | 本地 `phase/1` 有 126 项定向回归结果，未合入；种子 tags/assets 等待具名人工审阅 | 不自动补人工标签；golden hash 属后续验证。 |
| P1 Data / Catalog / Normalizer | ADR-0075/0076/0100/0108 已实现：单元级提交、窗口复用、窄证明复核单元内容、无元素归档判不完整 | 无代码缺口；E1-CAP-1 待在 `main` 上重跑（含预填充历史） | 容量结论只来自正式探针；D-META-AGE 待 K 轴数据 |
| P1 Dataset / Quality | v3 生产入口（ADR-0101）与契约 2.6.0 旧质量表绑定规则（ADR-0109）已实现 | 无代码缺口；DQ-9 数值仍 OPEN | 不选 DQ-9 数值；v2 写路径保持禁用 |
| P2 State | run / show / list 与诊断报告（ADR-0102）已实现 | 无代码缺口 | 真实 Catalog `state.*` 建表是运行操作 |
| P3 Event / P4 Outcome | Provider / 存储 / 操作入口在主线，未验收 | 无已确认可直接执行的普通代码缺口；Catalog 建表是运行操作 | 依赖 Phase 1 数据；不以本计划代替运行操作或验收。 |
| P5 Strategy / P6 Matrix | 研究与验证代码在主线，未验收 | 未发现已批准的直接实现缺口；Profile 数值与 Promotion 是决策 / 验收门 | 不猜 Profile，不绕过 Promotion / Validation。 |
| P7 Discovery | 计划绑定、PREPARE 证据、拒绝审计、准入交接（ADR-0103）与持久化恢复（ADR-0110）已实现 | 无代码缺口；执行开关默认关 | 验收前不打开开关 |
| P8 Robustness / P9 Calibration | 主线有报告与合成校准实现，未验收 | 暂无确认的 Accepted-ADR 代码缺口；待调试项须有具体失败证据 | 依赖 W1；不得更改验证阈值或 Profile。 |
| P10 Router | paper deviation 绑定运行（ADR-0104）已实现 | 无代码缺口 | 只读 / 纸面边界不变 |
| P11 Research Loop | 运维收口（ADR-0105）已实现：Lifecycle 写入 CLI、基线导出、批量驱动、Dataset 版 operator、测试 | 无代码缺口；真实运行需部署与冻结 Profile | 不伪造旧输入 |
| P12 Evolution | 可选 replacement trigger 与审计视图已在主线，默认关闭 | 未发现已批准的普通代码缺口 | OOS→PAPER 仍需人工批准；不得开启运行开关。 |
| P13 Execution | 仅模拟执行在主线，LIVE 被拒绝 | 无代码缺口；实盘明确不在范围 | 不连接真实账户、不加入下单 / 凭据。 |
| P14 Migration | 参考回测引擎迁移目标与演练（ADR-0106）已实现 | 无代码缺口 | 其他迁移目标另立 ADR |
| Apps / shared APIs | 只读 API / Web 查询面在主线；PR #19 视图已合入 | P10 候选改动按模块归档；当前无单独确认的代码缺口 | 不增加未批准的写触发或运行控制端点。 |

## 当前可执行任务卡

任务来源、状态与完成条件的唯一清单见[剩余底层代码完成计划](2026-09-28-remaining-code-gaps.md)。下表是同一四项任务的模块执行视图；候选完成后仍需独立审查，不能仅凭分支测试宣称已合入主线。

| ID | 依赖 | 文件边界 | 并行限制 | 未来代码完成条件 |
|---|---|---|---|---|
| E1-ARCHIVE-REUSE（P1 Normalizer） | Accepted ADR-0100 §6 与 canonical scratch 决定；严格 D1 parser / object hash 语义不变 | `infrastructure/parser/binance_archive.py`、`infrastructure/revision/row_integrity.py`、`infrastructure/canonical/normalizer.py`、probe 与相应测试 | P1 单任务；不得与其他 E1、Dataset 或 `core/` 契约任务并行。保留一个 spool 上限，多归档情况逐个 parse / close | 同一归档跨 verifier 窗口只严格解析一次；spool 及输入临时文件使用 canonical scratch；所有拒绝 / lineage / row 验证行为不变；资源在 close / eviction 中释放；代码 review 与定向验证记录完成后，整合 main 上的完整 E1 正式矩阵达到原定门槛，才可报告容量结果。 |
| E1-RAW-WINDOW-REUSE（P1 Normalizer） | ADR-0075/0100 的 bounded scan 与 E1-CAP-1；先复核 E1-ARCHIVE-REUSE | `infrastructure/canonical/normalizer.py`、必要 scratch/index helper、normalizer / row-integrity tests 与 probe | P1 单任务；与所有 E1 / Dataset / `core/` 契约任务串行；禁止把全日 Raw rows 保留在内存 | Raw 源只作有界确定性读取并为 proof / canonical windows 复用；排序、位置唯一性、lineage 和拒绝语义不变；所有 scratch 在成功 / 异常 / close 时释放；之后另做正式容量测量。 |
| E1-V2-REPLAY（P1 Dataset） | 主线 Accepted ADR-0077 DQ-10；以主线 v2 持久化 manifest 为输入 | `infrastructure/dataset/builder.py`、Dataset v2 / golden compatibility tests | 与 Dataset / `core/` 契约改动串行；不得新增 v2 writer | 历史 v2 manifest 通过公开重放入口正确关联 selection snapshot；snapshot 不匹配 / 缺失 fail closed；兼容 golden 用例通过；提交中记录实际验证范围。 |
| P7-CS-EXEC（P7） | 主线 Accepted ADR-0088 / 0099 / 0100 | `research/hypotheses/typed_plan_compiler.py`、`research/hypotheses/typed_plan.py`、`plugins/features/p7_cross_sectional.py`、对应测试 / P7 文档 | 独立 P7 工作；不得与 E1 / `core/` 契约并行；不得实现横截面到单序列的隐含转换 | 横截面根计划在显式 pinned universe 下编译和构造专用 Provider；非法根 / 跨类型输入被拒；运行开关默认关闭；测试覆盖成功与拒绝路径，真实检查结果有记录。 |

## 依赖与派工顺序

1. 逐文件审阅 Claude worktree 与 branch-only ADR，按净差异去重；P2 / P11 共享提交先拆边界。候选选择集 `2298 passed, 88 skipped` 仅归候选。
2. 串行收口 E1-ARCHIVE-REUSE 和 E1-RAW-WINDOW-REUSE；主线中断及候选诊断记录见 [2026-10-01 E1-CAP-1 partial](../reviews/2026-10-01-e1-cap1-main-partial.md)。
3. 分开审阅 E1-V2-REPLAY 与 P7-CS-EXEC；代码完成、review、整合、全仓验证、容量测量和 Phase 验收是不同状态。
4. 除上述四项外，不新增普通代码任务，除非对账确认 main 缺口且 main Accepted ADR / roadmap 唯一约束实现。人工、外部、待决事项留在阻塞清单。

## 状态定义

- **IN_MAIN_UNVERIFIED**：代码已在最新主线，相关验证 / 验收未完成。
- **IN_PROGRESS**：仅存在于未合入分支 / worktree，可能含未提交改动。
- **CODE_GAP**：主线缺失且可由主线已接受 ADR / 已批准 roadmap 唯一约束的实现。
- **BLOCKED / DEFERRED**：需要新决策、人工输入、外部配置或明确暂缓；不能视为可执行代码缺口。

---

## 历史记录（2026-09-28 计划及后续批次；保留原文，不再作为当前模块状态）

## 目标与边界

目标是把已批准范围内各 Phase 的逻辑、基础接线和项目文档补齐，统一留待 Raphael 安排测试与验收。代码进入 `main`、源码静态复核、测试通过和 Phase 验收是不同状态；本计划不把前两项写成后两项。

- 依据：`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/research/roadmap.md`、已 Accepted ADR 与 Claude 的 6 子代理只读模块审计（2026-09-28）。
- 不修改 `core/` 契约、Constitution、Validation Profile 数值、冻结阈值或 Phase 运行边界；此类需求先走 ADR / 人工决定。
- 不运行测试、build、lint、typecheck、probe、数据生成或阶段验收，除非 Raphael 后续授权。
- 并行任务必须各限于一个模块 / 插件；涉及 `core/` 的任务不得并行。协调者负责在整合前复核跨模块关系。
- 外部源工作区只读。分支整合采取择取已核实的提交；不整支合并从旧基线分叉、会回退 `main` 的分支。

## 当前代码线与分支处置

- 当前整合点：本地 `main@ae66dc8`；`origin/main@44fe9a2`；当前 ahead 131，未推送。四项 E1-R 提交已进入本地 main；状态文档待同步提交后预计 ahead 132。G1 两条边界测试仍未运行。
- `phase/1@52f7477` 的所有提交都是 main 祖先，没有独有 patch；已保存至 `refs/archive/2026-09-28/branches/local/phase-1` 后删除分支，并将根 checkout 切回 `main`。根下未跟踪旧计划已移入本地 `.codex/archive/`，跟踪版计划已在主线。Cursor 会话此前确认无独有实现；IDE 进程未强行关闭。
- `docs/research-spec-completion@82bfe73` 从旧 `20bdd82` 分叉，整树与当前 main 有 124 个路径差异，包含主线后续实现的删除 / 回退。其 `7b9df59` 的 G1 测试与策略收益率修复已核对在 main；其余 README / library 文档以 main 为准。tip 由 archive ref 保存；确认工作区干净、特殊测试与 main 相同后，已关闭闲置 Claude 会话并清理 branch/worktree。
- 远端只保留 `main`；不得从 archive ref 恢复或整支合并失败的 E1 候选。E1 候选 `c3868dc` 超过 32 MiB 门槛的失败证据继续保留。
- E1 候选、D3E 审查、C05、gatewt、E1 integration、B52、Codex E1-R 和 Claude research-spec worktree 均已清理，相关恢复点保留在 archive refs。Claude worktree 无未提交内容，唯一测试差异已与 main 核对相同；闲置 Claude 会话已关闭。清理阶段仅保留 root `main`；上一轮未提交的 E1 余项（ADR-0075 Amendment 1 草案、ADR-0076/0077 草案及 bounded scan/result 代码）已于 2026-09-28 经 Raphael 批准整体归档到 `wip/e1-cap-archive@ece150e`（archive ref `refs/archive/2026-09-28/branches/local/wip-e1-cap-archive`），未审阅、未测试、不构成正式决定；E1-CAP-1 仍阻断，本轮后置。

## 模块状态总览

| Phase / 模块 | 代码是否已齐 | 仍缺的实现（仅代码） | 是否需新 ADR / 人工门 | 本轮是否动手 | 可并行？ |
|---|---|---|---|---|---|
| P0.5 Knowledge Base | CODE_COMPLETE / DEBUG_PENDING：检索、review 写入、过滤、seed loader 已实现 | 无已批准的可编码缺口 | seed tags/assets 需具名人工审阅；黄金哈希留统一验收生成 | 否：人工 / 验收门 | 否 |
| P1 D0–D4 数据基础链 | CODE_COMPLETE / DEBUG_PENDING：Storage、Catalog、Collector、Parser、Revision、PIT、Normalizer、REST / lineage 均有基础实现 | 本轮不扩 E1 有界状态；其余经审计无已批准代码缺口 | 历史 universe / volume bars 是规格门 | 否 | 可按单模块独立，但当前无任务 |
| P1 E1 / Dataset | FRAMEWORK：基础读取、selection、manifest、RSS 工具已存在；上一轮未提交的 E1 余项（ADR-0075 Amendment 1 草案、ADR-0076/0077 草案及 bounded scan/result 代码）已于 2026-09-28 经 Raphael 批准整体归档到 `wip/e1-cap-archive@ece150e`，未审阅、未测试、不构成正式决定 | 仍有 E1-CAP-1 / Dataset 有界结果工作，但本轮明确排除 | ADR-0077、容量门、32 MiB 证据；需后续单独处理 | **否：本轮跳过** | 否：本轮不派 |
| P2 State + Web diagnostics | CODE_COMPLETE / DEBUG_PENDING：执行器与 Python / Web diagnostics 字段已接线 | 无已批准的代码缺口；兼容与渲染属于后续调试 | 逐 kind Report DTO schema 需新契约决定 | 否：当前实现随本地修改收敛 | 是，若后续只改 Web |
| P3 Event | CODE_COMPLETE / DEBUG_PENDING：Provider、DSL、统计、定义及显式建表命令输出已实现 | 无；旧断言留统一调试 | 真正创建 Catalog 表需人工授权 | 否 | 是，单独命令模块 |
| P4 Outcome | CODE_COMPLETE / DEBUG_PENDING：store/source、转换辅助与验证流已实现 | 无已批准调用接线；现有纯转换函数不新造写路径 | 若增加新写入调用点，需先确认流程 / ADR 边界 | 否 | 否：无任务 |
| P5 Strategy / Validation | CODE_COMPLETE / DEBUG_PENDING：策略、回测、验证、Promotion、FailureRegistry 已实现 | 无已批准实现缺口 | Profile 数值与验证门冻结 | 否 | 是，模块间可分开调试 |
| P6 Matrix | CODE_COMPLETE / DEBUG_PENDING：矩阵运行与报告接线已实现 | 无已批准实现缺口 | 无 | 否 | 是，模块独立 |
| P7 Discovery | FRAMEWORK / DEBUG_PENDING：non-runnable parser、resolver、durable v4/v5、有限 operator 与纯 binding validator 已有 | 预期 lowered outputs 全集完整性尚无权威来源，故不补 producer / lowering | 新 ADR / 输出权威；六类算子保持关闭 | **否：本轮跳过** | 否：需先定语义 |
| P8 Retro audit | CODE_COMPLETE / DEBUG_PENDING：writer、API、Web 页面、fixture-writer registry 已有 | 无本轮可编码项；fixture ID / hash 更新属于后续生成与验收 | 不代替人工知识标签审阅 | 否 | 是，若后续只改 app |
| P9 Calibration | CODE_COMPLETE / DEBUG_PENDING：单 / 多标的校准与 G5 证据已实现；固定 Decimal context 已补 | 无已批准实现缺口 | 不选新生成器或数值 | 否：当前代码随本地修改收敛 | 是，限 synthetic_lab |
| P10 Router | FRAMEWORK：Paper、deviation、evidence、validation 已有 | deviation 尚未绑定声明范围；具体需先定义范围身份与兼容规则，当前不能安全写成代码缺口 | **需 ADR / 语义决定，本轮跳过** | 否 | 否：跨 Router 语义 |
| P11 Loop | FRAMEWORK / CODE_COMPLETE（已批准 synthetic-only operator）：durable loop、stale-writer guard、degradation evidence、有限 CLI 已有 | 无当前批准的代码项；权威 ACTIVE/source resolver、dataset operator、仓内 scheduler 未获准 | 需权威来源 / provenance 决定；外部 scheduler 既定 | **否：本轮跳过** | 否 |
| P12 Evolution | CODE_COMPLETE / DEBUG_PENDING：operators、proposal、replacement 与冲突拒绝已有；精确类型校验已补 | P12-LOOP 是明确暂缓项，不是遗漏代码 | 自动 proposal 需未来 ADR | 否：当前修改收敛 | 是，单独 research/evolution |
| P13 Execution | CODE_COMPLETE / DEBUG_PENDING：模拟执行、审计、kill switch、风险 replay 已有；准入前拒绝与原子 mark 已补 | 无已批准实现缺口 | 仅 simulated；实盘需单独授权 | 否：当前修改收敛 | 是，限 apps/execution |
| P14 Migration | FRAMEWORK / CODE_COMPLETE：golden、diff、rollback evidence 与不可变快照已有 | 无目标系统时不写 target adapter；现有 conformance 覆盖不等于迁移目标 | 目标系统与 golden data 需人工提供 | **否：本轮跳过** | 否：等待目标 |
| Apps / shared APIs | CODE_COMPLETE / DEBUG_PENDING：Reports API、只读 Web 查询面、多类页面已实现 | 无已批准基础接线缺口；P2 / P8 消费字段已同步 | report kind 版本化 DTO 需契约门；不增加写触发 | 否 | 是：后续可按单 app 页面并行 |

本表按现有模块计划、Wave A/C 交付记录与当前工作区代码盘点；“CODE_COMPLETE / DEBUG_PENDING”只表示批准范围内基础逻辑已存在，不代表测试或阶段验收通过。P1 E1 / Dataset 明确留到后续，不作为本轮代码缺口继续深挖。

### 后续模块调试顺序建议

1. P0.5 Knowledge Base：先核基础读取 / 检索，再由具名人员处理 seed tags/assets；人工审阅不并入代码测试。
2. P1 D0–D4：按 Storage / Catalog → Collector / Parser → Revision / PIT / Normalizer → D3 REST / lineage → D4 Quality 的数据依赖顺序逐层调试。E1-CAP-1 / Dataset 容量与 ADR-0077 路径单列，待本轮之后决策。
3. P2 → P3 → P4：State → Event → Outcome，先验证各 Provider 输出，再验证 Outcome 转换与最小验证接线。
4. P5 → P6：先单策略 / backtest / validation / Promotion，再跑状态 × 策略矩阵。
5. P7 → P8 → P9：Discovery 保持 non-runnable 边界，再核审计报告，最后校准证据。
6. P10 → P11 → P12 → P13：Router → Loop → Evolution → 模拟 Execution；P11 维持 synthetic-only / 外部调度，P13 不连实盘。
7. P14 与 Apps：提供迁移目标后验证 conformance / rollback；各 Apps 页面随其数据模块调试，最后做跨模块只读查询回归。

以上只是下一阶段建议顺序，不代表任何 Phase 已验收；每个模块调试结束后单独记录结果与剩余门槛。

### 本轮跳过的决定 / 人工门

| ID | 模块 | 待定问题 / 人工动作 | 本轮处理 |
|---|---|---|---|
| DP-P1-E1 | P1 E1 / Dataset | ADR-0077 与 E1-CAP-1 的后续实现和容量证据 | 本轮跳过；ADR 保持 Proposed，不继续扩写或实施 |
| DP-P0.5-REVIEW | P0.5 Knowledge Base | 由具名审阅者确认 seed tags/assets；验收窗口生成黄金哈希 | 本轮不代审、不生成 |
| DP-P3-CATALOG | P3 Event | 是否授权在真实 Catalog 创建 Event 表 | 本轮不创建 |
| DP-P7-OUTPUTS | P7 Discovery | lowered outputs 完整集合的权威来源与后续 producer/lowering 边界 | 本轮不实现 producer 或启用算子 |
| DP-P10-SCOPE | P10 Router | deviation 报告如何绑定声明范围身份及兼容版本 | 本轮不猜语义、不改阈值 |
| DP-P11-AUTHORITY | P11 Loop | ACTIVE、真实 source 与 metric 的权威来源 / provenance | 本轮不加 resolver、dataset operator 或仓内调度 |
| DP-APP-REPORT-DTO | Apps | 是否为各 report kind 建立版本化 payload DTO 契约 | 本轮不收窄现有 API payload |
| DP-P14-TARGET | P14 Migration | 迁移目标、目标环境与具名 golden data | 未提供目标前不写 adapter |
| HUMAN-PROFILE | P4/P5/P9 | D-09 / Profile 数值与验证阈值 | 保持冻结，不在代码中填默认值 |

## 已批准的实现批次

每个任务单独提交或按本地整合策略提交；实现代理只在指派的模块文件内工作。Codex 做源码复核、冲突处理和项目文档更新。未列明的跨模块修改暂停并回报 `ARCHITECTURE_DECISION_REQUIRED`。

### Wave A：不依赖新架构决定的基础逻辑

| ID | 模块 | 任务 | 文件边界 | 状态 |
|---|---|---|---|---|
| A1 | P7 hypothesis composition | 按 ADR-0073 §1 实现 pure binding validator：每个 ExperimentSpec 精确绑定一个批次 Hypothesis；Hypothesis 恰被一个 ExperimentSpec 引用；已提交的每个 lowered output 精确匹配其 ExperimentSpec 的直接依赖 ref/hash；重复、缺失关联、额外关联与错 hash fail closed。不得接入 PREPARE、TrialLedger、Runner 或改变 `runnable=False` | `research/hypotheses/plan_bindings.py`、同目录 README；不改 `core/` | 源码复核无直接绕过路径；缺权威预期输出清单，暂不能证明 outputs 集合完整 |
| A2 | P3 event operations | 在默认无副作用、`--apply` 只操作 `event.events` 的现有命令输出中加入 TableDefinition 的 `name@version` 与完整定义内容哈希；绑定或表范围不符时 fail closed | `infrastructure/event/create_event_tables.py` | 源码复核完成；既有命令测试仍期待旧输出，留待验收窗口同步；未运行 |
| A3 | P2 Web diagnostics | Web `StateDiagnosticsPayload` 增加可空/可省略的 `source_result_hash` 消费字段，与 Python 1.1.0 输出对应，同时保留 1.0.0 旧 payload 可读 | `apps/web/src/lib/stateDiagnostics.ts` | 静态复核补充：非空 `source_result_hash` 同步要求 64 位小写 SHA-256；测试与类型检查未运行 |
| A4 | P8 report fixtures | 将现有 `retro_audit` writer/报告种类加入 console fixture writer registry；不生成 fixture 文件、不改 report schema | `tests/research/reports/test_console_fixture_writers.py`、`apps/web/fixtures/README.md` | 源码复核完成；旧 1.0.0 JSON / fixture ID 登记留待验收同步 |

### Wave B：代码 / 文档状态对账

| ID | 任务 | 文件边界 | 状态 |
|---|---|---|---|
| B1 | 校正 ADR-0068 / 0073、ADR index、roadmap、状态页中 Proposed / 已实现 / operator 入口等过时措辞 | 仅事实状态与交叉链接；不得改 ADR 裁决 | Codex 统筹，Wave A 后进行 |
| B2 | 统一 Git 数字、P8 fixture 版本、P13 验收措辞、Phase 1 表数和 Phase 1 关闭条件的状态叙述 | `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、被核实的对应文档 | 逐条读源核实后更新 |
| B3 | 整理 E1-CAP-ARCH 设计包：分离可在现有契约内处理的持有量与需新 ADR 的 metadata / retention 变化；记录失败候选数据的真实范围 | `docs/reviews/`、Proposed ADR 草案 | 首轮对账见 [`2026-09-28-e1-cap1-design-reconciliation.md`](../reviews/2026-09-28-e1-cap1-design-reconciliation.md)。E1-R 已在本地 main 有 batch-history、pinned batch reads、磁盘 positions、committed-row / close streaming、避免第二份 committed ID 列表、Verifier 快照匹配计数基础实现；公开 `snapshots_of_batches` 的 dict-of-lists 接口保持兼容。**批接口不等于内存有界**：PyIceberg 0.12.0 的 planner/task/delete 状态尚未证明有界；默认 tempfile 在本机落入 tmpfs。均未验证。32 MiB 完整工作集门槛不变；archive parse、完整结果 ID tuple、generic history fallback 与 Iceberg metadata 上界仍待处理或测量；E1-API 若改变公开结果类型先起 Proposed ADR，E1-H 需 L/H 分离证据。 |

### Wave C：额外模块静态审计发现的契约内缺口

| ID | 模块 | 任务 | 文件边界 | 状态 |
|---|---|---|---|---|
| C1 | P12 evolution | mutate 搜索空间成员校验改为精确类型和值；combine 对会沿用父代同一 `name@version` 的子代在创建前 fail closed，不猜版本演进规则 | `research/evolution/operators.py` 单模块 | 静态复核通过；未运行测试 |
| C2 | P13 execution | 未准入 deployment 必须在调用任何注入 target-position source 之前拒绝 | `apps/execution/service.py` 单模块 | 静态复核通过；未运行测试 |
| C3 | P14 migration | `GoldenRecord` 持有深度不可变输出快照；比较前验证 baseline 内容哈希；`GoldenDiff.differences` 复制、检查并冻结，防止报告结果被外部映射改写 | `infrastructure/migration/golden.py` 单模块 | Codex / Claude 静态复核未发现阻断项；未运行测试。公开类实例化的其他字段仍由 compare/rollback 的消费路径校验 |
| C4 | P9 docs | 把 ADR-0042 implementation note 对照现行代码更新为准确事实，保留当前报告证据规模仍仅适于 smoke 的限制 | `docs/adr/0042-synthetic-market-provider.md` | 事实同步已完成，待文档统一提交 |
| C5 | P9 calibration | false-positive rate / power 点估计使用固定 28 位 `ROUND_HALF_EVEN` context，不受调用方 ambient Decimal context 影响；不改阈值、报告字段或校准含义 | `research/synthetic_lab/calibration.py`、同目录 README | 静态复核完成；测试留后 |
| C6 | P13 second-line risk | 标记价格整批复制并预先校验，再更新 PositionBook；无效批次不得留下部分 mark 状态 | `apps/execution/risk.py`、同目录 README | 静态复核完成；测试留后 |

### 暂不实施，留到语义决定或验收窗口

- P0.5：不可替人工写 tags/assets/review；09-26 种子黄金哈希要留到允许运行 Python 的验收窗口。
- P1：E1-R 按对账文档开始基础实现；当前本地 main 增加 committed ID 重建与有界 snapshot match index，但仍未验证。archive parse、完整 result ID tuple、generic history fallback 的 `seen` set 与 builder 的 snapshot-ID set 仍在 E1 容量审计范围；D-LIST / ADR-0051 与新数据范围保留原门禁。
- P4/P5：不冻结 D-09/Profile 数值，不改变成本、split、threshold 或 outcome 选择语义。
- P7：六类组合算子、producer、execution lowering、失败修复与重试入口均待单独 ADR。
- P10/P11/P14：分别等待 deviation scope、ACTIVE/source resolver、migration target；P13 roadmap 验收措辞已与 ADR-0046 对齐。
- 所有已有测试文件缺口 / 兼容测试 / fixture 生成 / phase acceptance：集中留到后续验收批次；本阶段不运行。

## 并行与交付规约

- A1、A2、A3 可并行，因为分别属于 `research/hypotheses`、`infrastructure/event`、`apps/web`，不接触 `core/`。
- A1–A4 已完成源码复核，未运行测试 / build / lint / typecheck；C1–C3 由不相交模块并行处理，随后统一整合状态文档。

### 2026-09-28 跨阶段复核结论

- Phase 0.5–7：独立审计没有发现额外可在不触发人工审阅、运行验收或新架构决定的前提下直接编码的逻辑缺口。P4 converter 虽无生产调用点，但当前没有批准的运行时接点；保留纯函数，不新建写路径。P1 infrastructure Protocol / 测试代理接线已在 `34b95c3` 完成。ADR-0075 已接受，限定由 adapter 实现固定快照流式扫描；扫描实现尚未完成，E1-CAP-1 仍阻断。
- Phase 8–14：P10 deviation scope、P11 权威 ACTIVE/source resolver/dataset operator、P14 migration target 仍需语义或目标决定；P13 roadmap 已按 ADR-0046 修正为现有模拟功能范围，不需扩 ADR-0046。
- P10 只读审计确认 paper deviation 未绑定 ADR-0043/roadmap 所说的 P8 声明范围；需要先定范围身份与兼容策略，不增门槛。P11 只读审计确认 ADR-0074 synthetic-only 有限批次 CLI 已实现，缺口是未来的数据源真实性与 ACTIVE 权威语义，外部 scheduler 是既定边界。P13 roadmap 已对齐 ADR-0046。P14 无迁移目标前不写 target adapter；通用 GoldenRecord / GoldenDiff 不可变性已补到 C3，Claude 只读复核无阻断项。
- Phase 1 E1-CAP-1：对账文档列明 positions、committed columns、archive parse、batch index 和 result IDs 的代码持有量，以及 metadata / manifest 的未测边界。已加入 committed ID 重建与 Verifier 磁盘快照计数；`snapshots_of_batches` 公开 dict/list 接口保持不变。当前开发分支已将 `scan_column_batches` 加入 infrastructure `RevisionCatalog` Protocol，并给 revision `ProxyCatalog` 与 manifest-cache `_Recording` 代理补了转发 / head-read 记录；静态复核中、未运行检查。normalizer 的旧测试仍引用已删除的 `_same_numbers`。批次读取 API 的 PyIceberg planner、并发 task 和 delete 状态未证明有界；本机默认 tempfile 路径是 tmpfs。完整结果 tuple 仍计入 32 MiB 门槛；archive parse、generic history fallback 与 builder caller-held set 仍有未解决增长项。main 尚无容量证据，候选失败数据不外推。
- 每个开发代理一次只领一个 ID；Codex 复核后才进本地 `main`。Claude 可用最多 6 个子代理并行，但不能跨写同一模块。Cursor 此前确认 `phase/1` 没有独有实现；相关分支和 worktree 已归档清理。
- 交付须含：改动文件、任务 ID/Phase、对应 ADR/ROADMAP 约束、静态复核结果、未解决事项。测试、类型检查、lint、build 和验收状态统一标为“未运行”。

## PM 任务板（2026-09-28 起）

Raphael 于 2026-09-28 指定 Claude Code 以 PM 身份协调本轮：只有 PM 对 Raphael 汇报；Codex（`gpt-6-luna`）担任 Tech Lead，只执行 PM 签发的 Task Packet，不自设平行 GOAL；sonnet 子代理做只读审计与小切片修补，一人一任务、一个文件边界；涉及 `core/` 的任务串行。全员不跑测试 / probe / 验收（Raphael 2026-09-28：全部代码完成后再统一调试）、不宣称 E1-CAP-1 通过、不 push。Raphael 2026-09-28 决定所有模块底层代码都要补齐（含 E1），不得在单个模块死循环；新 ADR 由 Codex 依既有授权决定。子代理不能直接写根 checkout；PM 在自己的 worktree 分支整合后，由根 checkout 以 `--ff-only` 快进。

| ID | 模块 | 目标 | 执行者 | 文件边界 | 禁止项 | Done 定义 | 状态 |
|---|---|---|---|---|---|---|---|
| CL-1 | Git | 上一轮 21 个未提交 E1 路径整体归档，phase 分支恢复干净 | PM（仅 git 操作） | 根 checkout git 状态 | 改内容、stash、push | `wip/e1-cap-archive@ece150e` + archive ref；`git status` clean | ✅ |
| CL-2 | Docs | 挑回非 E1 事实同步（C5/C6、P9/P13），E1 叙述改为“余项已归档、后置”，记录协调方式 | sonnet | STATUS、MEMORY、本计划 | 代码、ADR、把 ADR-0076 写成 Accepted | 仅文档单提交 | ✅ `8a0adc4` |
| CL-3 | PM | 任务板落档（本节） | PM | 本计划 | — | 已提交 | ✅ |
| AUD-1 | 跨模块 | 静态列出测试与已提交实现的漂移（调试入口清单），只读不修 | sonnet | 无写入 | 运行任何检查、改测试、评估 E1 归档 | 每条带文件:行号证据 | ✅ 见下方清单 |
| W1-E1 | P1 E1 / Dataset | 审阅择取 `wip/e1-cap-archive`，定稿 ADR-0075 Amendment 1 / 0076 / 0077 并实现有界代码；单轮时间盒 | Codex | infrastructure E1 路径与测试、ADR-0075~0077 | 容量探针、宣称 E1-CAP-1 通过、弱化完整性验证、core/ | ADR 状态明确、代码与测试引用一致（未运行） | ✅ `5332034`：ADR-0075 A1 / 0076 Accepted 并实现，修 M3、补 M11 测试；ADR-0077 因需改 core 阻塞 → Raphael 2026-09-28 批准 DQ-1 = A |
| W1-P7 | P7 Discovery | ADR-0078：lowered outputs 权威；producer / 完整性校验 | Codex | research/hypotheses | 改 `runnable=False`、运行时启用算子、core/ | ADR + 实现 + 测试更新 | ✅ `79ea546` |
| W1-P10 | P10 Router | ADR-0079：deviation 绑定 P8 声明范围 | Codex | research/router | 改阈值、推翻 P10-FREEZE、core/ | ADR + 实现 + 测试更新 | ✅ `1974610`；console fixture 需运行 writer 重生成，列入调试 |
| W2-P11 | P11 Loop | ADR-0080：ACTIVE / source / metric 权威与 resolver | Codex | research/loop、research/operations | 仓内 scheduler、伪造 Profile | ADR + 实现 | ⛔ BLOCKED `b7ee4b1`：缺上游权威定义（D-P11-AUTH）；首次运行因 stdin 挂起浪费 4h 后重跑 |
| W2-DTO | Apps | ADR-0081：各 report kind 版本化 DTO；同步 deviation 2.0.0 | Codex | research/reports、apps/api、apps/web/src | DTO 进 core/、生成 fixture、收窄旧版读取、写 API | ADR + 实现 + 待重生成 fixture 清单 | 🔄 |
| W2-P4 | P4 Outcome | 按 roadmap 判定 converter 调用接线 | Codex | research/outcomes、research/experiments | 改 outcome 语义 / 阈值 | 接线或书面依据 | ✅ `317e6c8`：无需生产调用点，依据写入 README |
| W3-P7OPS | P7 Discovery | ADR-0082：六类算子语义与 lowering | Codex | research/hypotheses | 设 runnable、接 Runner | 逐算子 ADR + lowering | 🟡 `29763f3`：interaction 完成；其余五项 OPEN（D-P7-OPS） |
| W3-E1DS | P1 Dataset | 实施 ADR-0077（2.3.0 additive 契约模型 + 有界 Dataset API；v2 只读兼容） | Codex | core/contracts（仅新增）、infrastructure/dataset、catalog 新表 | 改动既有 v2 模型字段 / 哈希；与其他任务并行 | 实现 + 测试更新 | ⏳ 待当前任务全部结束后单独串行（涉及 core/） |
| W4-P7RETRY | P7 Discovery | ADR-0083：失败轮次审阅后的显式重试入口 | Codex | research/loop（不含 dataset_*）、apps/worker/loop.py | 删除 / 隐藏失败记录、重置 trial 计数、自动重试 | ADR + 入口 + 测试更新 | 🔄 |
| W3-P11D | P11 Loop | dataset operator + 在 ADR-0077 新 API 上再试一次权威解析（仅一次） | Codex | 待 W3-E1DS 完成后签发 | — | — | ⏳ 排队 |
| HUMAN | P14 / P0.5 | 迁移目标与 golden data；seed tags/assets 具名审阅 | Raphael / 具名审阅者 | — | 不代写、不猜 | — | ⏸ 需人工输入 |
| AUD-2 | 跨模块 | ADR 0001–0074 实施一致性审计 | sonnet ×3（含 4 个子审计） | 只读 | — | 逐 ADR 覆盖 | ✅ GAP: NONE；需决定项汇入 STATUS §6 |
| CR-1 | 复核 | Cursor 独立复核 Codex 提交 | Cursor（auto） | 只读 | — | — | ✅ 发现 CR1-2 已修 `ecf8b3b`；CR1-1 fixture 列入调试 |
| DBG-* | 各模块 | 逐模块调试 | — | — | — | — | ⏸ Raphael 决定：全部代码完成后再开始 |

### 调试入口清单（AUD-1，2026-09-28，静态只读，PM 抽查 M1–M3 属实）

覆盖 `44fe9a2..9e9bb9e` 各实现提交触及的模块。以下不是本轮实现缺口：修正需要运行测试或 writer 才能验证（M2 须重新生成 fixture），统一放到逐模块调试阶段。

| ID | 模块 | 位置 | 不一致 | 判断 |
|---|---|---|---|---|
| M1 | P3 Event（A2） | `tests/infrastructure/event/test_create_event_tables.py:48,78` ↔ `infrastructure/event/create_event_tables.py:66-75` | 测试桩无 `.definition`，仍断言旧输出；命令已输出 `name@version` 与 `definition_hash` | 确定漂移 |
| M2 | P8 retro_audit fixture（A4） | `tests/research/reports/test_console_fixture_writers.py:284-301` ↔ `apps/web/fixtures/retro_audit/05545674….json` | writer 已是 1.1.0，已提交 fixture 仍为 1.0.0 | 确定漂移；需运行 writer 重新生成 |
| M3 | P1 Normalizer | `tests/infrastructure/canonical/test_normalizer.py:932-933` ↔ `infrastructure/canonical/normalizer.py:1858` | 测试引用已删除的 `_same_numbers`，现为签名不同的 `_same_index_numbers` | 确定漂移 |
| M11 | P1 row_integrity | `infrastructure/revision/row_integrity.py:293-406` | SQLite spool / newest-match / close 路径无任何测试覆盖（经三次自我修正） | 覆盖盲区，调试时补测试 |

其余 8 项（P2 Web hash 校验、P9 Decimal context、P13 mark / admission、P12 精确类型、P14 golden、RevisionCatalog Protocol 代理、mixed-symbol 校验）静态核实与测试一致，部分新分支尚无覆盖。

### 代码补全轮次结果（2026-09-28 收尾，phase `0cfddbf`）

Raphael 于 2026-09-28 授权 PM 全权决策（实盘除外）。PM 编排 Codex 与子代理，完成了以下代码。**全部未运行测试。**

| 范围 | 完成内容 | ADR |
|---|---|---|
| P1 E1 / Dataset | 有界快照扫描、Canonical 摘要、v3 有界 Dataset 全链路（生成器、evidence 树、chunk、双表、流式 verify、下游消费者、公开访问器） | 0075 A1、0076、0077 |
| P1 上市历史 | 假设叠加层第一、二期（`POLICY_TABLE` 待联网核实后填入） | 0051 |
| 契约 | 2.3.0 → 2.4.0（均 additive） | 0077、0088 |
| P1 / P2 研究库 | 20 个特征、2 个状态、`bar_close` / `bar_high` / `bar_low` | 0085 |
| P4 | 波动率缩放三重屏障，以及 PIT 波动率接线 | 0088 |
| P5 / P8 | 3 个策略、组合策略、回撤风控、峰值权益、流水线接入 `run_with_risk`、门集完整性、报告阈值核验、Outcome 输入拒绝、Failure Registry 查询 | 0085、0086、0088 |
| P7 | outputs 权威；六类算子纯 lowering（rank / quantile 仍 OPEN）；失败轮次 durable 重试（v6） | 0078、0082、0083 |
| P9 | GARCH / 跳跃合成效应 | 0088 |
| P10 | deviation 绑定 P8 声明范围 | 0079 |
| P13 | 实盘接口预留（关闭） | 0084 |
| 退役记录 / 插件 | 退役记录存储；插件 Manifest 与 entry-point 发现 | 0086、0087 |
| Apps | 报告 DTO 版本化（至 2.4.0），Retro Audits 测试 | 0081 |
| 仍 BLOCKED | P11 ACTIVE / source / metric 权威解析 | 0080 |

### 调试入口清单（下一阶段）

1. 按模块运行 pytest / ruff / mypy。每次只跑一个模块，挂 `systemd-run` 内存上限。
2. 重钉契约升版带来的断言：2.3.0 / 2.4.0 版本号、`PRE_B3_SCHEMA_SHA256`、`V2_SCHEMA_SHA256_AT_2_2_0`、各「Re-pinned for 2.2.0」的回归哈希；执行 `python -m core.contracts.registry` 重新导出，与手写 schema 做 diff。
3. 运行 writer 重新生成 fixture：`paper_deviation` 2.0.0、`retro_audit` 1.1.0（AUD-1 M2、CR1-1）。
4. `uv sync`：使 ADR-0087 的 entry points 生效。Uvicorn 安装（ADR-0063）已在授权范围内。
5. 钉定新 Catalog 表的 golden 哈希与 ADR-0051 `ASSUMPTION_BINDING` 的哈希。
6. 在真实 Catalog 创建 `event.*` / `state.*` 表（已授权）。
7. E1-CAP-1 容量测量（小规模，外推），并确定 DQ-9 参数。
8. 联网核实 BTCUSDT / ETHUSDT 最早的 1m 归档日与 `exchangeInfo`，作为新版本填入 `POLICY_TABLE`。
9. 知识库种子的 tags / assets 需具名人工审阅；P14 迁移目标待提供。

## 阶段目标

1. 清理确认无活动会话的冗余分支/worktree，保留历史 archive ref 与失败证据。
2. 完成 Wave A 逻辑代码和 Wave B 文档对账，逐模块追加后续发现的已批准基础任务。
3. 形成明确的 handoff：哪些模块已实现但未验收、哪些仍待人工/ADR/数据门，以及建议的统一验收顺序。

本计划本身不表示 Phase 0.5 或 Phase 1–14 任一验收通过。
