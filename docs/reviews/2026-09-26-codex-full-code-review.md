# Codex 全代码分支复核与执行决定（2026-09-26）

## 范围与基线

- 复核对象：`claude/2026-09-26-code-completion-337e38`，基线 `d62c602`（B18）。
- 该提交与 `origin/wip/all-code-completion` 一致；检查时工作树干净。
- 本记录只做方向、缺陷和验收要求；代码仍由 Claude 实现。本复核不代表全项目通过或任何 Phase 已验收。
- B18 的计划记录了 `npm test` 43/43 与 `npm run build` 通过；但没有真实后端浏览器验收。最近一次全量非 PostgreSQL门禁基线是 `8d0d26d` 的 5861 passed、136 deselected；它早于 B15–B18，不覆盖当前 HEAD。

## 复核发现与决定

### K1 — 知识库 `verify` 对崩溃残留返回成功

`plugins/knowledge/cli.py` 的 `_verify()` 在只存在 `.review`、没有对应 `.json` 时只打印 `incomplete add`，`main()` 最后仍返回 0。用独立临时目录实际复现：

```text
incomplete add (review without item): item-crash.review
0 items load cleanly
exit_code=0
```

**决定：** `verify` 必须将任何未配对的 review 记录视为非干净状态并返回非零；同一内容通过 `add` 恢复崩溃写入的能力保留。增加回归测试，断言 CLI 返回值、错误输出和同内容恢复路径。

### K2 — 批次状态与待办文档已过期

截至 B18，计划 §10.2 仍把 P05-WRITE、P10-ELIG、P3-EVTABLE 列为未决；同一计划已分别记录 B15、B16、B17 的实现。调试待办 §F 仍将 P05-WRITE 和 P10-ELIG 列为未实现。`PROJECT_STATUS.md` 与 `PROJECT_MEMORY.md` 也仍描述较早的框架状态。

**决定：** Claude 下一个文档批次必须统一更新计划执行表、待办、项目驾驶舱和长期记忆。完成项要指向对应提交及实际验证范围；`CODE_COMPLETE / DEBUG_PENDING` 不能写成“项目完成”或“Phase 已验收”。保留未解决限制，不以改文字替代实际门禁。

### K3 — ADR-0052 的契约版本要求与自主决策记录矛盾

- [ADR-0052](../adr/0052-validation-contract-completion.md) §4 要求从 `2.0.0` 升至 `2.1.0`，并明确要求在发生重放破坏时先停止、报告，不得自行选择。
- `docs/reviews/2026-09-26-autonomous-decisions.md` 选择保持 `2.0.0`；`core/domain/base.py` 与 `docs/architecture/02-domain.md` 当前仍显示 `2.0.0`。
- 同一份决策记录因此不能替代对 ADR-0052 重放条件的证明或对正式 ADR 的修订。

**决定：** 遵循当前 Accepted ADR 的 `2.1.0` 要求。旧 `2.0.0` 记录和哈希必须原样可读、可重放；不得重写或删除已有行，也不得仅为避免冲突而把新增契约字段伪装成 `2.0.0`。实施前增加真实持久化载荷 / 规范化重放回归，证明旧对象仍按旧版本读取，且重复处理已提交输入不制造内容漂移。若当前共享版本常量无法表达此兼容，先设计显式的旧版本重放路径；只有在证明存在架构级阻碍后，才提出 superseding ADR，不得自行偏离现有 ADR。

**复核后的实施授权（2026-09-26）：** Claude 的 core lane 提交 `cc30ece` 实测证明：已提交为 `2.0.0` 的 Canonical 单元，在只把行构造版本提升到 `2.1.0` 后，`check_batch_snapshot` 以不同内容拒绝；当前版本重放则幂等。这使 ADR-0052 §4 的停止条件实际触发。为继续完成已批准的契约，我授权下一批在**独立 Phase 1 工作分支**实现兼容，不把契约版本留在 2.0.0，也不放弃 ADR-0052。接受路线为 ADR-0052 blocker §4 中“按持久化记录版本重放旧对象”的设计；实现者可提出更简单且同样满足以下不变量的方案，先提交设计证据再改代码。

- 旧 2.0.0 行、载荷、哈希、快照和 PIT 读取保持不变；任何历史行不得覆写或删除。
- 重放旧规范化单元时按该单元已提交的版本重建并比较；新合同对象按 2.1.0 写入；同表新旧记录的读取与 PIT 行为明确且有测试。
- 规范化规则身份 / 版本必须准确反映任何影响其输出身份的改变；不得在同一规则版本下生成内容不同、来源相同的 Canonical 重复行。
- 门禁至少包括：strict-xfail 旧单元重放转为通过、同表新旧版本混合、D-NET 已提交数据重放、PIT 读取 / manifest 哈希回归、契约 2.0.0 黄金向量逐字节不变、Schema 与 lint / types。
- 此工作跨 `core` 与 Phase 1 基础设施，必须等当前 core/contracts lane 停止修改这些目录后，由单一 lane 执行；不与 P3 / P6 的活动改动并行。保持独立 Phase 1 分支，不合入候选或 `main`，直到 Phase 1 阻断复核完成。

**关联契约决定：** ADR-0054 若含新增领域契约字段，同样必须审查是否适用 2.1.0 规则及既有哈希 / 重放兼容；不能以此前在 2.0.0 下实现作为版本规则豁免。

### K4 — Phase 1 PIT 边重复映射仍是独立验收阻断项

此前对跨日 R3 复核发现，公开 `PitSelector.select` 结果可含重复 `edge_id`：旧实现定向回归测试为 1 failed、7 passed；独立修复分支上对应测试与 selector 回归为 57 passed。修复尚未进入 Phase 1 候选分支，也没有候选分支上的门禁证据。

**决定：** 不接受 Phase 1，直到修复按拥有者公布的 push checkpoint 集成到候选分支，定向回归、相应 Phase 1 全量门禁和代码复核均通过。全代码分支不重复修改 PIT 文件。

### K5 — 不增加 `EventRequest.subject`，当前保持逐序列请求

Claude 的自主决策日志称“ADR-0057 Accepted”，并称要给 `EventRequest` 加可选 `subject`；但仓库没有 ADR-0057，`core/contracts/event.py` 当前也没有该字段。全代码计划 §10.2 的 P3-MULTISYM 则推荐保持每个序列一个请求，由调用方组合。事件请求已经能携带多个来源不同的 `EventInputPoint` / 上游事件，并以输入 lineage 复核来源；尚无足够定义说明新的单一 `subject` 对跨标的事件究竟表示什么。

**决定：** 选择计划中的方案 B：保持已发布 `EventRequest` 不变，不增加 `subject`，每个序列各自请求，再由上层按明确的交互定义组合。此决定不改变事件表 `event.events`。Claude 必须更正自主决策日志和计划中关于“ADR-0057 已接受 / 正在实施”的说法，不创建一个事后追认的 ADR。若未来产品需求要求跨标的事件的组合身份，先提交明确的主体 / 身份 / 可见性语义 ADR，再实现。

## 持续边界

- Phase 13 继续限于模拟 / 纸面；不接交易端点、不读取账户凭据、不下单。
- Validation Profile 的实际数值不以猜测冻结；必须留给有代表性的校准与项目所有者确认。
- 研究策略不绕过 Promotion 晋升为生产插件；没有完整证据就保留在研究层。
- 不把未复核的全代码分支合入 `phase/1` 或 `main`，不打发布 tag。

## Claude 下一批执行顺序

1. 等当前正在跑的 P6 / P8 任务完成，要求各 lane 报告分支、提交、测试原始结果与文件范围；逐个集成并在每次里程碑后提交、推送 WIP。
2. 修复 K1，加入失败状态回归和恢复回归，运行 knowledge 定向测试、lint、类型检查，提交并推送。
3. 同步 K2 所列文档；更新 `PROJECT_STATUS.md` 固定 12 节结构和 `PROJECT_MEMORY.md` 固定 9 节结构，检查行数与职责规则，再提交并推送。
4. 按 K3 的规则实施 ADR-0052，先实现并验证旧版读 / 写兼容，再继续依赖这些字段的阶段；不能证明兼容时保留旧行为并交付具体阻断证据。
5. 不合并 Phase 1。所有后续工作保持 `CODE_COMPLETE / DEBUG_PENDING`，直到相应完整验证完成并由 Codex 独立复核。

## 本轮验证记录

- `plugins.knowledge.cli.main(["verify", "--items-dir", <临时目录>])`：存在孤立 `item-crash.review` 时实际返回 `0`，复现 K1。
- 复核时全代码工作树 HEAD `d62c602`，与 `origin/wip/all-code-completion` 相同，工作树干净。
- 复核时存在两组 Claude 子任务 pytest 进程仍在运行；因此未在共享依赖 / 机器负载上重启全量测试。
- 本记录不主张当前 HEAD 已通过完整非 PostgreSQL门禁。

## 后续复核增补（2026-09-26）

- **K1 接受**：Claude 提交 `e254a26` 将孤立审阅记录改为 `KnowledgeWriteError`，退出码为 1，并保持同内容 `add` 恢复。Codex 在该提交后的分支独立运行：`uv run --offline pytest -q -p no:cacheprovider tests/plugins/knowledge` → `50 passed in 0.12s`；`ruff check plugins/knowledge tests/plugins/knowledge` → `All checks passed!`；`mypy plugins/knowledge tests/plugins/knowledge` → `Success: no issues found in 8 source files`。
- **P8 暂不接受**：`fc273d8` 增加多标的验证，静态设计符合保守通过条件；需等集成回归结束，并复核测试结果与 G1 多标的负对照风险处理。该功能仍为 `CODE_COMPLETE / DEBUG_PENDING`。
- **P6/P11 暂不接受**：`a21000d` 增加可选条件假设登记并接入持续循环。集成 pytest 仍在运行；验收须核对循环哈希兼容、失败/重启路径、试验数与预算计数，再审核明确记录的“逐条件验证未做”限制。
- **P3-MULTISYM 决定**：不实施 `EventRequest.subject`，保持逐序列请求；自主决策日志须移除不存在的 ADR-0057 接受声明。
- **K2 部分完成**：`b2ea7b0` 更新全代码计划 §10.2 与调试待办 F，使 B15–B20 状态较一致；截至复核时尚未更新 `PROJECT_STATUS.md` 与 `PROJECT_MEMORY.md`。另外 B19 计划文字称自主决策记录已按 K3 取代 ADR-0052 的 2.0.0 选择，但 `b2ea7b0` 的文件清单不含 `docs/reviews/2026-09-26-autonomous-decisions.md`，需直接核对其正文后再声称已同步。
- **Git / 门禁**：Claude 本地 HEAD 为 `a21000d`，远端 `origin/wip/all-code-completion` 仍为 `efd631e`；P6/P11 和 B20 文档之后的提交尚未推送。运行中的合并回归完成后应按用户要求提交并推送检查过的恢复点。全量非 PostgreSQL门禁仍未在 `a21000d` 或之后的 HEAD 上运行。
- **后续状态**：core lane 随后提交 `cc30ece`，提供 ADR-0052 2.1.0 重放失败证据与 proposed compatibility design；Codex 已按上文授权独立 Phase 1 实施路径。P3 lane 的 `113f6da` 含 ADR-0057 / `EventRequest.subject`，和 K5 相反，当前仅在其私有工作树，明确禁止集成。

## 复核增补：P3-MULTISYM 语义与 2.1.0 版本门（2026-09-26）

复核 Claude 全代码工作树 `dd2bc6a` 及未提交的 `event.events` 表适配后，更新 K5 的**语义结论**：新起草的 ADR-0057 现在明确规定“一请求对应一个标的”、请求 / 上游 / 结果的标的一致性，并把标的纳入请求、事件及结果身份。这比最初 K5 时缺失的语义具体；不同标的下相同事件不再争用同一 `event_id`，因此采用可选 `EventRequest.subject` 有清楚的功能理由。保留方案 A 的产品语义，但多标的仍须逐标的运行并由上层组合；不授权单个请求承载多个标的。

**实现当前仍不接受，也不应推送到 WIP：** ADR-0057 和代码把新增领域字段作为契约 `2.0.0` 发布，理由是缺省字段不改变旧哈希；然而“不改变旧哈希”不能证明新字段可被旧版 `2.0.0` 读者理解。ADR-0057 自己也承认严格的旧读者会拒绝带 `subject` 的同版本载荷。这与本复核 K3 对已接受 ADR-0052 的决定直接冲突：新增契约字段不得伪装成 `2.0.0`，应先解决版本化重放兼容，再以 `2.1.0` 写入新对象。

执行顺序：

1. 将此 ADR-0057 / `EventRequest.subject` 实现留在隔离提交或工作树，不合入 `wip/all-code-completion`、Phase 1 候选或 `main`；撤回仍在活动 core/contracts 目录的表改动，或等待该 lane 明确停止并提交可恢复点。
2. 在独立 Phase 1 分支完成 K3 授权的按持久化记录版本重放兼容，先通过 K3 所列旧 2.0.0、同表混合版本、D-NET、PIT / manifest 与黄金向量门禁。
3. 在已解决的版本机制上重定基 ADR-0057：新 `subject` 对象标记为 2.1.0，旧对象仍按已持久化版本读取 / 重放；增加旧读者拒绝或兼容行为、跨版本往返及哈希测试，并同步所有计划、ADR 索引、状态和记忆文档。
4. 重新运行相关 Phase 3、Canonical / D-NET / PIT、Schema、lint、types 与完整非 PostgreSQL门禁。通过前状态仍是 CODE_COMPLETE / DEBUG_PENDING，不宣称验收。

因此，原 K5 中“缺少产品语义，保持无 subject”的理由由 ADR-0057 补足后不再维持；原 K5 对当前实现不予接纳及不得集成的约束仍有效，约束基础现明确为 K3 的契约版本一致性，而不是否定逐标的 subject 的功能价值。该增补不修改 ADR-0052 的执行授权或 Phase 1 验收门。

### 独立只读交叉复核发现

- `subject` 的功能价值成立，但身份约束仍需写清：当前定义是“标的 / 序列名”，实现允许任意非空字符串。ADR-0057 应明确它是稳定规范标识，或说明它是由调用方负责稳定性与别名一致性的 opaque key；这影响 `request_hash` / `event_id` 可复现性。
- `infrastructure/event/table_definition.py` 把 `subject` 插为 Iceberg field ID 10，并将原运行块字段 ID 后移。只有在确认所有共享 / 持久化 catalog 都没有建过旧 `event.events` 后，才可用“尚未部署”作为改变定义的理由；否则必须保留原字段 ID 并执行显式 schema evolution / 新定义版本。不能仅凭 ADR 文本声明未部署。
- `table_definition.py` 的模块说明仍称逻辑表九列、字段 ID 1–10、run block ID 10–14；与实际十列 / ID 1–15 不一致。ADR-0056 的合规清单也仍写 `core/`、`schemas/` 未修改，需随 ADR-0057 的实际改动更新。
- 独立检查本机仓库默认 `data/warehouse`：没有 `event.events` 目录 / Iceberg 元数据。当前 Codex shell 未设置 `HLENS_CATALOG_URI`，因此不能查询 PostgreSQL Catalog；此证据只说明默认文件 warehouse 尚无该表，不能证明所有 catalog 未部署。

这些是 ADR-0057 可以并且应该在恢复开发时解决的实现条件，不改变“逐标的 subject 有价值”的方向；在它们与 K3 门禁解决前，当前 `dd2bc6a` 与未提交物理表改动仍不进入 WIP。

**`subject` 身份决定：** 将其定义为调用方提供的稳定 opaque identifier，大小写敏感；Contract 的既有字符串规则会去除首尾空白，除此之外 core 不做符号大小写转换、别名映射或交易所推断。任何对应真实 Instrument 的 provider / 调用方都必须从稳定的 Instrument 身份生成该键，不得用可变显示名。此规则维持 Phase 3 通用事件契约，不把市场目录逻辑塞入 core。

### ADR-0052 版本化重放的实施方向

Canonical 表的每行已持久化 `contract_schema_version`；按 Raw revision unit 读取时，同一 unit 的已有行必须只有一个受支持版本。重放 / 补完一个已有 unit 时，以其固定 Canonical snapshot 中记录的版本构造预期行，再照常验证原 batch fingerprint、row count 与逐列相等；任何已提交行或 snapshot 都不改写。新 unit（尚无提交批次）使用进程当前 `CONTRACT_SCHEMA_VERSION = 2.1.0`。部分提交的 unit 也必须沿用已提交行的唯一版本补齐；若已存在 batch snapshot 却找不到任何版本行，或 unit 内版本混杂 / 未知，则 fail closed。

这要求把版本显式贯穿 `CanonicalNormalizer._survey` / `_planned` / `_verify_batches` / `_write` 和 `rules.canonical_row`，不能只在 2.1.0 运行时全局读取常量，也不能通过放宽 batch fingerprint 或跳过 `_exact` 比较来“兼容”。验收除现有 strict-xfail 转绿与 2.0.0 黄金向量逐字节不变外，还须有：同一表中 2.0.0 与 2.1.0 不同 unit 共存；旧版本部分提交后的恢复；已提交 D-NET 数据在重放后 snapshot / manifest 不变；PIT 对两版记录均可读取且选择哈希不变；同 unit 混版与无版本证据均 fail closed。实现需验证这一路径确实覆盖持久化 Canonical 快照，而不只是内存 fixture 的模型序列化。

**M0 设计复核（`ed1a202`）：** Claude 提议用 `ContextVar` 作为重建既有契约对象的局部版本作用域，并已记录 24 步跨进程探查中 23 步在 2.1.0 失败、同版本对照 0 步失败；M0 仅有设计与固定测试数据，没有代码实现。我接受该机制作为实现方向，条件是：作用域只包住已持久化对象的重建 / 校验，严格用 `try/finally` 或 context manager 恢复，正常新调用不得继承历史版本；仅缺省 `schema_version` 可取作用域版本，显式版本及显式嵌套对象不得被改写；未发布版本、作用域泄漏、写入组内版本不一致必须 fail closed。Canonical `contract_schema_version` 列须与构造的 `RevisionRecord.schema_version` 同源，不能让二者分叉。此为对实施方案的认可，不代表 M1/M2/M3 或 ADR-0052 实现验收。

### 集成状态复核（2026-09-26）

Claude 随后提交并推送 `564c87c` 到 `wip/all-code-completion`，将 `dd2bc6a` 与 `event.events` 的未提交改动一并集成；工作树目前干净。该提交记录目标集成测试 `3889 passed, 1 xfailed`（596.24s）、`ruff check .` 通过、`ruff format --check .` 662 files、`mypy` 513 files 无问题、135 份 Schema 检查。这个测试命令覆盖主层测试、契约套件、plugins、部分 research / infrastructure 与 apps 路径，**不是**全仓 `pytest -m "not postgres"` 最终门禁，也未由 Codex 独立重跑。

当前 `core/domain/base.py` 仍为 `CONTRACT_SCHEMA_VERSION = "2.0.0"`。因此即使该批测试按提交记录通过，ADR-0052 / K3 仍未实现；新 `subject` 字段继续在 2.0.0 下发布，不能据此接受为契约完成。`wip/all-code-completion` 现在可作为保留的恢复点，不应据此合入 `phase/1` 或 `main`。后续必须在独立 Phase 1 分支完成 K3 持久化版本重放兼容，再以 2.1.0 修正 ADR-0057 / ADR-0054 的版本化声明和 Schema，补齐 Iceberg 字段 ID / 定义版本的兼容证据、计划和状态文档，并运行全仓最终门禁。

文档同步仍有具体缺口：完成计划 §10.2 的 P3-MULTISYM 行仍把 B 标为推荐，状态更新又说 ADR-0057 正在实施；B23 已把 ADR-0057 / 物理表改动推入 WIP。`PROJECT_STATUS.md` 和 `PROJECT_MEMORY.md` 仍只列 B1–B21，未纳入 B22/B23，也仍引用早于这些批次的 `5861 passed` 基线。全仓最终测试仍在进行，B23 的 3889 项目标门禁不能替代最终全量结果。测试结束后须统一修正这些记录并引用确切提交与命令。

### 当前状态文档复核补充（2026-09-26）

对 `claude/2026-09-26-code-completion-337e38` 的 `564c87c` 再核对后，K2 文档同步仍未收敛，且物理表说明有可直接证实的错误：

- `docs/plans/2026-09-26-all-code-completion-plan.md` §10.2 的 P3-MULTISYM 行仍推荐方案 B，但本文件已接受 ADR-0057 的逐标的 opaque `subject` 语义；同节紧随其后的状态更新仍称 ADR-0057 “在 core 通道实施中”，而该分支已包含其实现。
- 同表 P05-WRITE、P10-ELIG、P3-EVTABLE 仍以未决问题格式保留，尽管本节状态更新称 ADR-0058、P10 证据模式、ADR-0056 已集成。状态说明不能替代把主表改成已解决结论。
- `PROJECT_STATUS.md` 与 `PROJECT_MEMORY.md` 仍把全栈批次记为 B1–B21；当前 WIP HEAD 是 `564c87c`，已含事件 `subject` 与物理事件表修改。状态文件仍把 `5861 passed`（`8d0d26d`）列作最近全量门禁，而它早于这些批次。
- `infrastructure/event/table_definition.py` 顶部说明称逻辑事件表有九列、字段 ID 为 1–10，run block 为 10–14；当前定义已有 `subject` 这一第十个逻辑列，run block 实际使用 11–15。此模块文档与代码不一致，应随最终 Schema / ADR 文档同步修正。
- 当前复核时，`564c87c` 的全仓非 PostgreSQL pytest 与另一工作树 `50cdc49` 的 Phase 1 pytest 仍是运行进程；当前没有它们的终态结果。不得将旧门禁或目标测试写成当前 HEAD 全量通过。

**执行决定：** Claude 应在全量门禁结束后统一修复以上状态矛盾和物理表说明，引用实际 commit SHA、原始门禁命令与结果；在此之前保持 `CODE_COMPLETE / DEBUG_PENDING`。这属于必要文档一致性收尾，不改变 ADR-0052 / K3 的实现方向，不授权合入 `phase/1` 或 `main`。

### 全量门禁结果更新（2026-09-26）

此前记录的非 PostgreSQL 全仓门禁已结束。Claude 的运行记录显示命令为 `uv run pytest -q -m "not postgres" -p no:cacheprovider`，结果为：

```text
6142 passed, 136 deselected, 1 xfailed, 1 warning in 2112.48s (0:35:12)
```

运行期间 HEAD 为 `564c87c`。结果优于旧 `5861 passed` 基线，但不覆盖 PostgreSQL 标记测试；也不代表 Phase 1 验收或 ADR-0052 已实现。测试结束后 `claude/2026-09-26-code-completion-337e38` 已出现计划 / ADR / 事件表定义文件的未提交修改，因此该结果不能证明这些新修改已通过门禁；提交后至少重跑文档一致性与受影响的事件表检查。Phase 1 的 `50cdc49` 全量门禁与 ADR-0052 的基础设施测试在本次更新时仍运行中，结果待各自终止并核验。

### ADR-0059 方向决定（2026-09-26）

Codex 复核 Proposed ADR-0059 与现有 G4 实现后，决定：**接受选项 C 作为当前修复；不接受选项 A 作为本批实施内容。** 现有 `cross_asset_check` 只有 `per_asset: Mapping[str, PeriodReturns]`，因此无法从收益序列本身证明策略在单标的重跑时“没有敞口”。实现须从每个单标的 `TrialRun` 的实际目标仓位中记录零敞口事实；不可只因净收益为零、没有成交或策略名称而推断“不适用”。仅当全部已声明标的的独立重跑均有全零目标仓位时，C-R3 给出具名 `INCONCLUSIVE`，绝不 `PASS`；该情形不进入按正收益比例作出的 `FAIL`，因此不会因检查结构不适用而写入否定性 Failure Registry。单标的检查中的任何一个存在非零目标仓位、数据缺失、或不满足该精确条件时，维持现有 C-R3 路径、阈值来源与判定；现有 C-R3 / C-R5 的其他门不变。

ADR-0059 的选项 A **不在本批授权内**。将来若要实施子宇宙检验，须另提 ADR，先定义不依结果或收益挑选的确定性、不相交划分规则、最小可检验资产数、分组及跨组报告语义和多重检验计数；策略是否属横截面应来自显式、可审计的策略声明，不得从表现推断。此决定不改冻结契约、Schema、既有阈值或单标的 / 时间序列策略结果。

### ADR-0059 实现复核后的决定修订（2026-09-26）

在看到实现提交 `7703fba` 后，Codex 修订上一节“本批不授权 A”的决定：**接受 ADR-0059 的 C + A 方向**。实现为横截面策略使用静态、显式登记的 `StrategySpec.name` 集合，不从策略表现推断；子宇宙由排序后的唯一标的按固定连续分组规则产生，互不重叠且每个至少两个标的；少于四个标的无法产生两个子宇宙时为 `INCONCLUSIVE`；沿用原有阈值和 trial count。单标的 / 时间序列策略继续走原路径。该方案满足本复核要求的确定性分组与“不足样本不判 PASS”，因此此前仅推迟 A 的顾虑已被具体实现和报告语义解决。

此修订批准的是方向，不是对提交的验收。Claude 提交说明记录该 lane 293 项定向测试通过、ruff / format / mypy 通过；B29 文档记录 350 项集成检查。Codex 尚未独立重跑这些命令。合入全代码 WIP 前仍需：核验最终分组报告 / 哈希、确认不足资产数及未声明策略 fail closed、核验原时间序列与单标的 golden hashes、让当前集成测试和最终全仓门禁在已提交 HEAD 上通过。ADR-0059 不增加任何 Profile 数值，不改变其他 G4 门，也不代表 Phase 8 已验收。

### B45 Phase 9 配置错误分类复核（2026-09-26）

**结论：B45 暂不接受，发现一个需要修复的统计正确性问题。** 将配置 / 输入拒绝从 detector error 中传播出来是正确方向；`PROPAGATED_ERRORS == research.validation.g4._PROPAGATED` 的漂移保护、单标的 / 多标的 / G5 路径及报错范围测试也覆盖了本次分类修改。

阻断点在 `CalibrationReport.false_positive_rate_bounds` 与 `power_bounds`（当前 `research/synthetic_lab/calibration.py:63-77`）：端点直接用 `Decimal(numerator) / trials`，依赖默认 `ROUND_HALF_EVEN`。因此这些声称覆盖未知错误样本结果的上下界不保证向外舍入。已在项目环境核验：`Decimal(2) / 3` 得 `0.6666666666666666666666666667`，严格大于精确的 2/3；`Decimal(1) / 3` 得 `0.3333333333333333333333333333`，严格小于精确的 1/3。`trials=3`、2 个 false positives、0 个 noise errors 时，下界被抬高；1 个 false positive、1 个 noise error 时，上界可能被压低。

**要求**：使用明确的有理数比较或方向舍入，使 lower bound 永远 `<= numerator / trials`、upper bound 永远 `>= numerator / trials`；处理 `trials=1` 和整除情形；为 FPR 与 power 各加 repeating-decimal / outward-rounding 回归测试。不要只增加小数位数或依赖当前 Decimal context 精度。`ArmEvidence.pass_rate_bounds` 已有单独的 floor / ceiling helper，可作为一致性参考，但必须按 Phase 9 calibration 的报告精度与语义验证。

**具体实现决定：** `CalibrationReport` 是内存证据对象，没有序列化哈希依赖；保持现有字段与值类型 `Decimal`，对上下端点使用显式 `decimal.Context(prec=28)`，lower 以 `ROUND_FLOOR`、upper 以 `ROUND_CEILING` 计算。比率都在 `[0, 1]`，28 位有效精度足够且匹配该项目现有 Decimal 默认分辨率；整数结果（0、1、整除）仍保持精确。测试用 `Fraction` 对重复小数断言包络关系，而不是只固定一串 Decimal 字面量。不要借用 `intervals.PLACES` 的 6 位量化，因为这会不必要地改变该 API 现有的结果精度和“无 detector error 时为点值”的行为。

在当前 `0220f9a` 代码上直接构造 `CalibrationReport` 并用 `Fraction` 比较，仍复现：`false_positives=2, trials=3` 的 lower 为 `0.6666666666666666666666666667 > 2/3`；`false_positives=1, trials=3, noise_errors=0` 的 upper 为 `0.3333333333333333333333333333 < 1/3`。这不是只对 Decimal 表达式的推断，而是当前属性的实际返回值。

**P9 中等规模证据状态核实（本次复核）：** 隔离 worktree `agent-af8c34b0c82f4c7d8` 仍停在 `c36005b`，只有未提交的 `evidence_setups.py` / `test_evidence_setups.py`，`docs/research/calibration/` 下没有产物；没有正在运行的 `run_cli.py` 进程。scratchpad `single.log` 记录第一次运行 `exit=143`，第二次只留下 `15:28:54Z` 的开始时间，没有终态或报告。故中等规模校准目前**没有可接受的证据结果**；应继续该隔离任务，将规模降至按计划能在约 20 分钟内完成的最大种子 / bar 数，明确记录实际种子数与 bar 数，生成并提交 evidence-only 报告后再计完成。不要从进程消失推断成功。

Claude 报告 B45 的定向 synthetic-lab 测试为 79 passed，ruff / format / mypy 通过；这些结果不覆盖上面的数值边界。修复并运行有针对性的校准测试后，再复核 B45；最终集成门禁仍需覆盖该修复提交。

### B46 API 错误路径脱敏复核（2026-09-26）

**结论：B46 的异常响应收敛方向正确，但 `public_detail` 仍有一个可复现的路径泄露缺口。** 新增的 `file:` URI 分支只匹配小写 scheme；URI scheme 按规范不区分大小写。使用当前提交 `7c91090` 的 `public_detail` 实测：

```text
failed file://host/home/raphael/private/report.json => failed report.json
failed FILE://host/home/raphael/private/report.json => failed FILE://host/home/raphael/private/report.json
failed FiLe://host/home/raphael/private/report.json => failed FiLe://host/home/raphael/private/report.json
```

因此当 provider 或底层异常以合法的大写 / 混合大小写 `file://host/...` 表示服务器路径时，catch-all 500 之前的 HTTP 错误脱敏仍会原样返回目录结构。修复时应让 `file:` scheme 匹配大小写不敏感，同时保留 URI authority 与路径的正确边界；为全小写、大写、混合大小写及现有 `file:/`、`file:///`、`file://host/` 形式增加测试。确认 URL `https://host/path`、比例 `1/2` 和普通文本不被误改。完成后在 B46 所在集成 HEAD 重跑 API 定向测试与 lint / types，再纳入完整门禁。

此发现来自对 B46（`7c91090`）代码的只读复核和项目环境下的直接运行；Codex 未修改 Claude 的实现分支。此前 B45 的 outward-rounding 统计正确性阻断仍未解决，不因本次 B46 复核而解除。

**门禁范围核实补充：** 当前观察到的全量非 PostgreSQL pytest 进程运行于 `.claude/worktrees/agent-af9bb980640458bdd`，其 HEAD 为 `949cef8`（F13 架构边界测试）；`81161a1` 不是该 HEAD 的祖先。因而即使此进程通过，结果也不覆盖 B44、B45、B46 和其后的代码 / 测试，不能作为当前 `wip/all-code-completion` 最终门禁。所有并行通道集成、B45/B46 修复提交后，仍须在最终集成 HEAD 上重跑完整门禁并记录该确切 SHA。

### B49 Phase 12 外部锚点并发复核（`b3a68e4`）

**结论：ProposalLedger 的同账本锁有效，但公开的 `ProposalAnchor` 没有对锚点文件自身加锁或刷新缓存；两个账本共用同一路径时可直接破坏锚点哈希链。** `AppendOnlyJournal` 在构造时把 entries 缓存在内存，`append()` 不重新读取文件。`ProposalAnchor` 持有此对象，`publish()` 先从该缓存 `load()`，再基于同一缓存追加。

在临时目录中先以两个 `ProposalLedger` 路径创建账本，二者共享同一个外部 anchor，再分别调用 `record()`（proposal 不同）。**两个 `record()` 都成功返回**，但 anchor 已有两行、第二行仍是 `seq=1`；随后新建 `AppendOnlyJournal(anchor)` 返回 `JournalCorrupted: ...:2 has the wrong sequence number`。独立的 `ProposalAnchor(p)` stale-cache 测试也得到相同结果。不同 ledger 路径分别持有各自的 `<ledger>.lock`，因此共享 anchor 不互斥；公开 API 会对外报告成功，留下损坏的审计锚点。

**架构决定：** `ProposalAnchor` 是一份账本的外部锚点，不设计为多账本汇总器。给 anchor 自身使用独立的 OS `flock` 锁，并在每次 `load` / `publish` 的锁内从磁盘新建并验证 `AppendOnlyJournal`；发布新 head 时必须将锚点当前 head 与目标账本的上一条已验证前缀（count + hash）比较，只有匹配才追加。账本重开时也只能从已核实的共同前缀向前补锚。两个不同账本意外共用一个 anchor 时，后来的写入必须明确拒绝，不能写入第二条 seq=1，也不能用成功返回掩盖分叉。不能只给缓存对象加进程内互斥。增加两个独立 ledger 共用 anchor 的并发 / 陈旧实例回归测试，验证任何已返回成功的 publish 后，新的 `AppendOnlyJournal(anchor)` 都可完整重放，且 count/head 不回退、不分叉。修复前，含 `ProposalAnchor` 的 B49 不应接受。B45 Decimal 边界与 B46 大小写不敏感的 `file:` 脱敏缺口也仍待修复。

Codex 在 `02aacb7` 上独立运行：`pytest -q -p no:cacheprovider tests/research/evolution/test_replacement_proposals.py tests/research/evolution/test_replacement_job.py` → **39 passed in 7.76s**；`ruff check research/evolution tests/research/evolution` → **All checks passed**；`mypy research/evolution tests/research/evolution` → **Success: no issues found in 10 source files**；API 报告 / live-smoke 定向集 → **42 passed, 1 StarletteDeprecationWarning in 2.85s**。这些定向测试通过，但前述 anchor 复现说明尚无共享锚点并发 / 陈旧实例覆盖。

### B50 Phase 7 / 持续循环边界复核（`0220f9a`）

Claude 增加了具名 `LlmContentUnverified`，使无法核验的调用保留 `LlmCall`、内容哈希和拒绝原因；其他 provider `ValueError` 不再被误报成草稿拒绝；空 prompt 在构造阶段拒绝。开启内容验证的循环状态把 `llm_content_verified` 纳入身份，重开时不能在验证开 / 关之间切换；数据集组合根增加 ConditionalPlan SQLite 测试目录端到端覆盖。这些变化保持未验证 provider 的既有 fingerprint 形状。

Codex 在 `0220f9a` 独立运行：`pytest -q -p no:cacheprovider tests/research/loop/test_llm_content.py tests/research/loop/test_loop_llm_rejection.py tests/infrastructure/e2e/test_research_loop_dataset_conditional.py` → **20 passed in 118.41s**；受影响源文件 `ruff check` → **All checks passed**；`mypy research/loop/compose.py research/loop/dataset_compose.py research/loop/llm_content.py research/loop/stages.py` → **Success: no issues found in 4 source files**。静态复核未发现新阻断。随后 Claude 将 B50 代码、B49/B50 计划记录一并推送为 `b773656`；计划记录该分支集成循环 / 假设 / 数据集端到端回归 **230 passed, 1 warning**，ruff / format / mypy 通过。B45、B46、B49 的独立阻断仍未解除。

## 当前执行顺序复核（基线 `b773656`）

本节根据 2026-09-26 对实际进程、工作树和远端分支的复核补充。Claude 的代码工作树干净，`claude/2026-09-26-code-completion-337e38` 与 `origin/wip/all-code-completion` 同为 `b773656`。Codex 未改动该工作树中的实现。

1. **完成 Phase 9 中等规模证据任务。** 隔离 worktree `agent-af8c34b0c82f4c7d8` 中，`single_instrument_evidence` 正在运行；观测到的配置为 250 个单标的种子，`run_cli.py` 进程仍在计算，输出目录尚无已核验的终态报告。不要按启动记录计为完成；任务结束后核对退出码、seed / bar 数、所有报告文件和内容哈希，提交 evidence-only 结果与实际可复现命令。若资源限制迫使缩小规模，报告必须标明实际规模和局限。
2. **修复 B45 校准置信界。** 在 `research/synthetic_lab/calibration.py` 中按前述明确决定对下界向下、上界向上舍入；用 `Fraction` 回归测试证实 FPR 与 power 在重复小数、整数比及 `trials=1` 时都包住精确值。不要只追加小数位或改成六位量化。
3. **修复 B46 错误路径脱敏。** 对 `file:` URI scheme 做大小写不敏感匹配，保留 authority / path 的边界；覆盖 `file:/`、`file:///`、带 authority、大小写混合，并验证 HTTPS、比例和普通文字不变。
4. **修复 B49 外部锚点。** `ProposalAnchor` 必须有跨进程锁，并在锁内从磁盘重新读取、验证日志；对不同账本误用同一 anchor 必须拒绝分叉。增加独立 ledger、陈旧实例和并发测试，不能让损坏日志的写入返回成功。
5. 三项修复分别提交并推送 WIP，记录每个 SHA 的定向测试与静态检查。合并后运行受影响的集成回归；最后的全量 pytest 必须在**最终集成 SHA**上运行。已观察到的旧全量进程位于 `949cef8`，无论它最终通过或失败，都不覆盖 `b773656` 及后续提交。
6. **同步驾驶舱与决策文档。** ADR-0055 的能力方向已由 Codex 依 Raphael 授权决定：接受 tag / exact-asset 检索方向，实施前先修订 ADR 和 2.2.0 版本 / 哈希兼容要求；`PROJECT_STATUS.md` 和计划不得再写“等待 Raphael 决定”，应写成“方向已定、ADR 修订和实现未完成”。ADR 仍保持 Proposed，直到规范和兼容测试证据完成。D-LIST / ADR-0051 仍明确暂缓；不调用 `exchangeInfo`。

当前可以并行处理 B45、B46、B49（各自独立模块），但遵守 `CLAUDE.md` 的单模块隔离约定；P9 证据任务继续留在其隔离工作树。任何提交不得将 `CODE_COMPLETE / DEBUG_PENDING` 改写成 Phase 验收或项目完成。Phase 1、`main`、release tag 仍需独立复核 / Raphael 明确批准。

## `1cd3284` 后的复核更新（2026-09-26）

Claude 随后将 B51 文档与集成结果推送到 `origin/wip/all-code-completion`，当前代码分支为干净的 `1cd3284`。在该 HEAD 上重新检查后，B45 / B46 / B49 仍未修复：

- **B45 直接复现：** 用项目 `.venv` 构造 `CalibrationReport(trials=3, false_positives=2, ...)`，当前 `false_positive_rate_bounds[0]` 返回 `0.6666666666666666666666666667`；以 `Fraction` 比较，`lower <= 2/3` 为 `False`。
- **B46 直接复现：** `public_detail("failed FILE://host/home/private/report.json")` 与混合大小写 `FiLe://...` 都原样泄露完整路径；小写 `file://...` 正常脱敏。
- **B49 直接复现：** 两个目录不同的 `ProposalLedger` 实例共用一个外部 anchor，各自执行 `record()` 均成功返回；随后新建 `AppendOnlyJournal(anchor)` 抛出 `JournalCorrupted: ...:2 has the wrong sequence number`。

当次观察时，250-seed `single_instrument_evidence` 的 `run_cli.py` 仍在 CPU 计算，尚无终态报告；同时全量命令 `pytest -q -m not postgres -p no:cacheprovider` 已在 `1cd3284` 的工作树启动，观察时运行约 50 秒、尚无结果。两项均须按实际终态记录。即便该全量命令通过，它也不能解除上面三个已复现的缺陷；所有补丁合入后仍须在最终 SHA 重跑门禁。

## D-DEG-IE 已决策

B51 记录的 D-DEG-IE 不再等待 Codex：方向已由 Codex 决定，新增 `research_loop.degradation.insufficient_evidence` 独立告警主题，仅用于所有规则指标缺失时通知监控证据不足；不得发布为真实劣化，不触发生命周期动作。实现、ADR-0049 同步与验收标准见 [D-DEG-IE 决策记录](2026-09-26-d-deg-ie-codex-decision.md)。

## Phase 9 单标的中等规模报告终态复核

隔离运行 `single_instrument_evidence` 已于 2026-09-26 15:59:38Z 结束，`exit=0`；`/usr/bin/time` 记录 wall 15:28、CPU 927.38s、最大 RSS 207,508 KB。临时报告位于 Claude 会话 scratchpad（尚未进入仓库），SHA-256 内容报告标识为 `bada61369ed52c29eaaba1efffbf610f768fb325ee3a56e3f74fa56a33de4841`，文件名与 `report_hash` 一致；独立用项目 `content_hash` 重算报告 body 得到同一哈希。

实际输入 / 结果核验：每个噪声与植入效果臂各 250 个种子，共 3 个臂；基准合成市场为 4,320 分钟，`RandomWalkMarket` 每分钟生成一根 bar，因此每条 run 对应 4,320 根 bar。两个 TEST ONLY 候选各有 750 条 run，检测器错误均为 0；较宽松候选的 G5 开封 / 评估各 139 次，较严格候选为 0。报告明确保持 `FRAMEWORK_IMPLEMENTED / NOT_VALIDATED` 与“evidence only — not a Profile decision”，不作 Profile 参数决定。运行所用 factory/test 文件仍未提交，报告仍只在 scratchpad，须由 Claude 把 factory、可复现命令、报告、README 与规模限制一并提交到仓库后，才计入项目交付。

**B45 影响范围更正：** 上述 `GateCalibrationReport` 的 Clopper–Pearson 区间通过 `research/synthetic_lab/intervals.py` 以 `Fraction` 计算并分别向外量化；已查看报告中记录的 6 位区间，也与此路径一致。因此 B45 的 `CalibrationReport.false_positive_rate_bounds / power_bounds` 默认 Decimal 舍入缺陷仍须修复，但不污染这份 gate-calibration 报告的区间。B45 是独立缺陷，不应成为丢弃或重跑该报告的理由。

多标的 `multi_instrument_evidence` 随后在同一隔离任务中启动，采用 200 个种子 / 臂；当次复核时仍在运行，无终态输出。

## B46 修复复核：接受（`dd4fada`）

Claude 在独立分支 `fix/b46-file-uri-redaction` 完成大小写修复，并推送 `dd4fada`。变更只在 `_ABSOLUTE_PATH` 的 `file:` 前缀使用局部大小写不敏感组 `(?i:file)`；authority 与路径匹配分支保持原样。新增完整输出参数测试覆盖 5 种 scheme 大小写 × 6 种 URI / 混排形式，并覆盖 HTTPS、大写 HTTPS、比例、普通文本、`profile://` 与真实 502 错误体。`apps/api/README.md` 与专属实现记录已同步。

Codex 在该提交独立运行：`uv run --offline pytest -q -p no:cacheprovider tests/apps/test_api.py` → **62 passed, 1 Starlette deprecation warning**；`uv run --offline ruff check apps/api tests/apps` → **All checks passed**。静态复核未发现 B46 范围之外的行为变化；B46 **接受为代码修复**，仍属 `CODE_COMPLETE / DEBUG_PENDING`，尚未集成，也不表示 API / Phase 验收完成。

## B45 修复复核：接受（`a32a9b6`）

Claude 在独立分支 `fix/b45-calibration-bounds` 推送 `a32a9b6`。`CalibrationReport.false_positive_rate_bounds` / `power_bounds` 共用局部 `Context(prec=28)`，lower `ROUND_FLOOR`、upper `ROUND_CEILING`；仍返回 `tuple[Decimal, Decimal]`，不改点估计、报告哈希或 `GateCalibrationReport` 的独立 Fraction 区间。新增 Fraction 精确包络与紧度测试，覆盖两项属性、重复小数、整除、`trials=1`、零 detector errors、ambient context 变化和真实 `calibrate()` 路径。

Codex 独立运行：`uv run --offline pytest -q -p no:cacheprovider tests/research/synthetic_lab/test_calibration.py` → **90 passed in 1.08s**；`uv run --offline ruff check research/synthetic_lab/calibration.py tests/research/synthetic_lab/test_calibration.py` → **All checks passed**；`uv run --offline mypy research/synthetic_lab/calibration.py tests/research/synthetic_lab/test_calibration.py` → **Success: no issues found in 2 source files**。未发现偏离 B45 约定的行为。B45 **接受为代码修复**，仍为 `CODE_COMPLETE / DEBUG_PENDING`，须与其他通道合并后跑最终全量门禁。

## D-DEG-IE 实施复核：接受为代码修复（`d389a39`）

Claude 已在独立分支 `claude/phase11-insufficient-evidence` 推送 `d389a39`，按已批准的 D-DEG-IE 决定新增 `research_loop.degradation.insufficient_evidence` 主题。实现区分实际阈值越限与全规则指标缺失：后者只发独立告警，payload 含 subject、window、status、排序后的 missing / required；部分缺失但无越限仍不发事件；没有 bus 时需要发出的两类事件都 fail closed。`check()` 仍纯函数，代码不接触生命周期 API，既有降级事件 payload 不变。ADR-0049 与 worker README 同步，独立实施说明明确未改契约、Profile、阈值与生命周期。

Codex 独立运行（worktree `claude/phase11-insufficient-evidence`）：`uv run --offline pytest -q -p no:cacheprovider tests/apps/test_research_loop.py` → **37 passed in 0.19s**；`uv run --offline ruff check apps/worker/degradation.py tests/apps/test_research_loop.py` → **All checks passed!**；`uv run --offline mypy apps/worker/degradation.py tests/apps/test_research_loop.py` → **Success: no issues found in 2 source files**。静态复核未发现超出决策范围的修改。D-DEG-IE **接受为代码修复**；仍属 `CODE_COMPLETE / DEBUG_PENDING`，尚未集成，须在最终 WIP HEAD 验证后才能记录为集成完成，不代表 Phase 11 验收。

## B49 外部锚点并发修复复核：接受为代码修复（`558b099`）

Claude 在隔离分支 `claude/fix-b49-anchor-lock` 推送 `558b099`。`ProposalAnchor` 现在对 anchor 自己持有跨进程 `flock`；每次 load / publish 都在锁内重放磁盘 journal，不再依赖陈旧对象缓存。ledger record 在 anchor 排他锁下重新核对所有锚定前缀，再写 ledger 行并推进 anchor；短链、分叉链、共享 anchor 的第二条 ledger 被拒绝且不落新行；同一条 ledger 的崩溃后超前链仍可在重开时恢复。测试覆盖最初的双 ledger 复现、独立进程的竞争发布、陈旧 ProposalAnchor 对象、anchor 篡改与合法恢复。

Codex 独立运行（worktree `claude/fix-b49-anchor-lock`）：`uv run --offline pytest -q -p no:cacheprovider tests/research/evolution/test_replacement_proposals.py tests/research/evolution/test_proposal_anchor_sharing.py` → **37 passed in 0.59s**；`uv run --offline ruff check research/evolution/proposals.py tests/research/evolution/test_replacement_proposals.py tests/research/evolution/test_proposal_anchor_sharing.py tests/research/evolution/anchor_child.py` → **All checks passed!**；`uv run --offline mypy research/evolution/proposals.py tests/research/evolution/test_replacement_proposals.py tests/research/evolution/test_proposal_anchor_sharing.py tests/research/evolution/anchor_child.py` → **Success: no issues found in 4 source files**。复核了锁与 journal 更新顺序及新增并发用例，未发现 B49 范围内遗留问题。B49 **接受为代码修复**；仍为 `CODE_COMPLETE / DEBUG_PENDING`，未集成；必须随 B45、B46、D-DEG-IE 等补丁集成后重跑全量门禁，不代表 Phase 11 验收。

## 全量门禁终态（基线 `1cd3284`）

**更正：** 此前把 `6785 passed` 的旧任务输出误记为 `1cd3284`。该输出来自 worktree `agent-af9bb980640458bdd`，其 HEAD 是 `949cef8`，不能证明 WIP 基线门禁通过。主集成工作树上单独启动的最终门禁日志 `scratchpad/final-gate-3.txt` 在启动前记录 `HEAD 1cd3284`；pytest 执行路径也位于该主工作树。终态为 `uv run pytest -q -m "not postgres" -p no:cacheprovider` → **6889 passed, 136 deselected, 1 warning in 3036.72s (0:50:36)**，退出码 **0**。这证明 `1cd3284` 的非 PostgreSQL pytest 门禁通过。B45、B46、B49、D-DEG-IE 当时尚未全部集成，因此它不证明后续集成 HEAD；这些补丁合入后仍需在最终 SHA 重跑全量门禁，并同步计划 / 状态文档。

## Phase 9 双报告证据包复核：接受为 evidence-only 交付（`5650358`）

隔离分支 `worktree-agent-af8c34b0c82f4c7d8` 提交 `5650358`，包含两个明确标为 `FRAMEWORK_IMPLEMENTED / NOT_VALIDATED`、`evidence only — not a Profile decision` 的报告，setup 工厂、输入哈希与复现说明。读取对应 scratchpad 原始运行日志确认：(a) 单标的 250 seeds / arm，`exit=0`，15:28.24，峰值 RSS 207,508 KB；(b) 多标的 200 seeds / arm，`exit=0`，19:18.76，峰值 RSS 226,568 KB。报告名与 `report_hash` 分别为 `bada61369ed52c29eaaba1efffbf610f768fb325ee3a56e3f74fa56a33de4841` 和 `0f04d1b649d7ce6357e47d84e6ae709d343bfcbb44271398a34cb2aa83f2cecd`。

Codex 独立运行：`uv run --offline pytest -q -p no:cacheprovider tests/research/synthetic_lab/test_evidence_setups.py` → **11 passed in 0.72s**；`uv run --offline ruff check tests/research/synthetic_lab/evidence_setups.py tests/research/synthetic_lab/test_evidence_setups.py` → **All checks passed!**；`uv run --offline mypy tests/research/synthetic_lab/evidence_setups.py tests/research/synthetic_lab/test_evidence_setups.py` → **Success: no issues found in 2 source files**。独立从提交报告的 run rows 重新计数，README 所列单标的 / 多标的 pipeline PASS/FAIL、detector error=0、G5 开封 / evaluation 数一致；已提交测试会校验两个 report hash、setup inputs hash 与实际 factory 输入相等。接受该提交作为可复查的 evidence-only 交付。**不**据此选择 Profile 数值、证明真实市场有效性或宣布 Phase 9 / 项目验收；`5650358` 仍需集成并由候选分支统一门禁验证。

证据包复核通过后，Codex 已将 `5650358` 推送到 `origin/claude/phase9-calibration-evidence`，作为可供主集成分支引用的恢复点；提交内容未改写。

## 集成复核补充：B52 CLI、B53 区间与 console 资格说明

- **B52 `93477c6`：接受。** `python -m research.synthetic_lab.gate_calibration` 过去会因 `__main__` 与已导入包模块的 setup 类身份不同而拒绝工厂。入口现转调包模块的 `main()`；新增真正子进程回归，README / evidence setup 使用说明同步。该测试包含在 Codex 对集成 SHA `21ae9d8` 的定向集 **376 passed, 1 warning in 70.78s** 中。
- **B53 `dd158c9`：接受为统计边界修复。** G5 模式端到端 PASS / power 的分母是该 arm 全部运行；G0–G4 或 G5 detector error 均使端到端结果未知，新增的 `end_to_end_bounds` 只在存在此类错误时输出，按 `[observed_passes / n, (observed_passes + errors) / n]` 向外舍入。G5 子区间仍只以到达 G5 的运行作分母；错误之外的报告序列化不变。Codex 独立运行 B53 + 关联输入测试：`uv run --offline pytest -q -p no:cacheprovider tests/research/synthetic_lab/test_gate_calibration_g5.py tests/research/synthetic_lab/test_gate_calibration.py tests/research/synthetic_lab/test_evidence_setups.py` → **64 passed, 1 warning in 79.29s**；`uv run --offline ruff check research/synthetic_lab/gate_calibration.py tests/research/synthetic_lab/test_gate_calibration_g5.py` → **All checks passed!**；对应 mypy → **Success: no issues found in 2 source files**。
- **`b1a3e08` Web / report 展示：部分待修，暂不接受。** Codex 在 `21ae9d8` 上运行的 Python 定向回归（含 console fixture、report writer）376 项通过；`npm test` 与 `npm run build` 均退出 0。静态复核发现 `checkStatus()` 对 `metrics=[]` 使用 `[].every(...) === true`，会把零指标 payload 错标为“全部指标缺近期值”，而 `asDegradationCheckPayload()` 目前接受空 `metrics`。另一个不一致形状也需拒绝：`insufficient_evidence: true` 但存在非 missing 指标。后端合法 monitor 至少需要一个 threshold，但 console 的输入解析器必须 fail closed。修复后补空列表与矛盾旗标测试，再由 Codex复核。

复核通过 B53 后，Codex 将集成候选 `ec2a8b02013895adebb7511f3e699d67f876917c` 快进推送到 `origin/wip/all-code-completion`。该 SHA 含 B45/B46/B49/B52/B53、D-DEG-IE 与 P9 evidence-only 报告；尚不含随后单独进行的 Web fail-closed 修复和 ADR-0055 tags/assets 实现，因此它不是最终门禁 SHA。

## Console degradation-check 解析修复复核：接受（`e46fc88`）

Claude 在隔离分支 `claude/fix-degradation-ui-empty` 修复前述 b1a3e08 边界。解析器拒绝空 metrics；有 `insufficient_evidence: true` 时要求所有 metric 均 missing、missing 名称列表与 metrics 精确一致，否则退回原始 JSON。没有该字段的旧报告仍按非空 metrics 推导全缺失状态，部分缺失语义不变。新增用例覆盖空数组、旗标 / 指标矛盾、missing 名称错漏或非字符串、旧版无 flag 全缺失。

Codex 独立运行：`npm test` → **105 tests, 0 failures**（lib 与 12 组件套件）；`npm run build` → TypeScript 两种配置检查通过，Vite 619 modules 构建成功。静态检查未发现不兼容真实 fixtures 的行为。接受 `e46fc88` 为代码修复；该提交已推送到 `origin/claude/fix-degradation-ui-empty`，尚未集成进 `wip/all-code-completion`，也不代表 Phase 11 验收。

B52 CLI 复现补强：Codex 在集成分支 `ec2a8b0` 用 README 的正式模块入口重新运行 `single_instrument_evidence`，生成文件与提交报告 `single_instrument_evidence.json` 逐字节相同；`cmp` 输出 `BYTE_IDENTICAL`。命令为 `uv run --offline python -m research.synthetic_lab.gate_calibration --setup tests.research.synthetic_lab.evidence_setups:single_instrument_evidence --out <temporary-dir>`。这确认旧的 `-m` setup 类身份问题已经修复，且固定报告可用正式 CLI 重现。
