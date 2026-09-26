# ADR-0062: Validation Profile 冻结登记（file-backed、追加式）与 Promotion 的权威冻结来源

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-27，Codex 依 Raphael 授权，基于 B56 代码 `6e482e2` / `a89b00a` / `7f93629` 的复核、扩大门禁与文档检查接受）**；架构决定由 Codex 依 Raphael 全权授权作出（2026-09-27，B56；Q1 = A、Q2 = A）；ADR / 实现的接受**不等于** Phase 4 或 Phase 5 验收：冻结登记仍为空（没有任何 `profile.frozen` 记录，所有 Promotion 仍以 `profile_not_frozen` 被拒），Validation Profile 数值（D-09 TBD-1..5）仍未冻结 |
| 日期 | 2026-09-27 |
| 决策者 | Codex（Raphael 2026-09-23 授权的技术协调者） |
| 起草者 | Claude Code（Opus），只起草与实现，不改变决定 |
| 相关 Phase | Phase 4（Validation Profile 两步冻结）、Phase 5（ADR-0005 Promotion 链） |
| 影响范围 | Infrastructure（新增 `infrastructure/registry/profile_freeze.py`）；Research（`research/promotion` 的 Profile 检查）；**不**改 Domain Contract、Schema、Validation Profile 结构 / 数值 / 内容哈希、Constitution、ADR-0008 |
| 是否破坏兼容 | 否：新增独立登记；已有 Registry、Profile 与报告的字节与哈希不变。Promotion 变得更严（今天本就全部拒绝） |
| 前置 | [ADR-0005](0005-research-production-boundary.md)（Promotion 链）、[ADR-0007](0007-validation-architecture-three-layers.md)（两步冻结）、[ADR-0008](0008-contract-payload-immutability.md)（`status` 不进内容哈希）、Constitution C-A5 / C-A8 |

## 背景（Context）

- ADR-0008 决策 3：`ValidationProfile.status` 是操作状态，**不进** `content_hash()`；`provenance`（含 `calibration_report`）留在哈希内。
  因此同一份阈值内容的 `draft` 与 `frozen` 对象哈希相同，报告的 `validation_profile_hash` 无法说明 Profile 是否已冻结。本 ADR
  **不改变**这一决定。
- Promotion（`research/promotion/service.py::_check_profiles`，ADR-0005 实施说明第 2 条、C-A8）今天以调用方交来的 Profile 对象的
  `status is FROZEN` 且 `provenance.calibration_report` 非空为冻结依据。`status` 由调用方任意构造，不受任何哈希或记录约束——
  这不是权威来源。
- ADR-0007 的两步冻结与 D-09 规定 Profile 数值由 Raphael 在校准后冻结；冻结本身是一次人工批准，需要可审计、只追加的记录。
- 生产 Control Plane（PostgreSQL，D-01 / D-02）不在当前范围；现有 Strategy Registry 已采用 file-backed、哈希链、单写者锁、
  目录外锚点的替身模式（ADR-0005 实施说明第 1 条）。

## 决策（Decision）

1. **独立的 `ProfileFreezeRegistry`**（`infrastructure/registry/profile_freeze.py`，Q1 = A）：一个目录，内含
   `freezes.jsonl`（`infrastructure.event_bus.journal.AppendOnlyJournal`，同一哈希链 JSON-lines 磁盘契约）、`blobs/`
   （`infrastructure.registry.blobs.BlobStore`，按 SHA-256 命名、只写一次）与 `.lock`（`fcntl.flock` 单写者）；以及**必须提供**的目录外锚点
   （独立的哈希链锚点日志，`(length, head)` 语义与 Strategy Registry 相同）：构造登记时锚点路径是必需参数，位于登记目录内或
   缺失即拒绝打开。整行截断只有锚点能发现，因此没有锚点的登记不能作为 Promotion 的权威证据（Codex 复核，2026-09-27）。直接组合这些既有组件；**不**重构、不修改
   `StrategyRegistry` 及其锁 / 锚点逻辑。只依赖 `core` 与 `infrastructure`（不 import `research/` 或 `apps/`）。
2. **唯一记录类型 `profile.frozen`**，载荷恰好含以下键（多、少一律拒绝）：
   - `format_version`：记录格式版本，本 ADR 定义 `"1.0.0"`；其他值拒绝；
   - `profile`：Profile 的完整 JSON（`model_dump(mode="json")`）；`profile_ref`（`kind:name@version`）；`profile_hash`（`content_hash()`）；
   - `calibration`：`{"kind": "gate_calibration", "report_hash", "sha256", "uri"}`——校准报告的类型、自哈希、原始字节 SHA-256 与 blob URI；
   - `approved_by`：具名批准人（非空，去首尾空白后不变）；`approved_at`：UTC 时间（带时区的 ISO 8601，规范化为 UTC）；
   - `freeze_id`：除 `freeze_id` 外全部键的规范 JSON 的 SHA-256。
3. **登记时固化并验证校准证据**（Q2 = A）：调用方交来 Profile 对象、校准报告**原始字节**、批准人与批准时间。登记前全部检查：
   - Profile 重新校验（无 `model_construct` 捷径），`status is FROZEN`，`content_hash()` 即记录的 `profile_hash`，ref 一致；
   - 报告字节是 UTF-8 的规范 JSON 对象（与 `research/reports` 写出的文件逐字节一致；非规范字节拒绝，不重新序列化）；
     `kind == "gate_calibration"`；`report_hash` 等于去掉 `report_hash` 键后的规范 JSON 的 SHA-256（报告自哈希成立）；
   - `profile.provenance.calibration_report` **逐字等于**该 `report_hash`（校准工具的约定："a frozen Profile may cite this report's hash
     in provenance.calibration_report"）；
   - 原始字节以只写一次的 blob 固化，其 SHA-256 即记录的 `sha256` 与 URI。
   **不**要求报告的 `candidate_profiles` 含最终冻结的 Profile：回填 `provenance` 本身就改变 Profile 哈希，而"候选与冻结版本数值
   相同"如何判定没有既有 ADR。报告是否适用于该 Profile，由具名批准记录承担——它是"已审阅该报告适用于该 Profile"的人工声明。
4. **追加与重放执行同一套规则**，任一不成立即拒绝；重放时整个登记无法打开（fail closed）：未知记录类型、多余 / 缺失键、错误
   `format_version`、`freeze_id` 与内容不符、Profile 不能校验 / 非 FROZEN / 哈希或 ref 与记录不符、`provenance.calibration_report`
   与记录的 `report_hash` 不符、校准 blob 缺失 / 被篡改 / 非规范 / 类型不对 / 自哈希不成立、批准人为空、批准时间非 UTC；
   **重复**（同一 ref + 哈希再次登记）与**冲突**（同一 ref 已以另一哈希冻结）拒绝。日志本身的篡改、断链、部分尾行（崩溃残留）、
   文件缩短由 `AppendOnlyJournal` 判为损坏。整行截断由必需的目录外锚点发现（登记短于锚点记录的长度、或锚点所见位置的哈希不同
   → 损坏；登记比锚点多出一条以上 → 损坏）；记录已写入、锚点未写入是唯一合法的崩溃窗口，打开时补写锚点。
   没有任何编辑、删除、解冻或 supersede 记录；本批不定义这些操作。
5. **Promotion 以登记为权威**：`research/promotion` 在构建 Artifact 时必须拿到一个已打开（已重放、已校验）的 `ProfileFreezeRegistry`
   只读视图（按第 1 条必然带锚点）；每份报告的 Profile 除 `status is FROZEN` 外，还必须在登记中有恰好对应 `(profile_ref, profile_hash)` 的 `profile.frozen`
   记录，且记录的 `report_hash` 等于该 Profile 的 `provenance.calibration_report`。没有有效登记 → 类型化拒绝 `profile_not_frozen`
   （即使对象声明 `status = FROZEN`）；`profile_not_calibrated` 等既有拒绝码不变。今天登记为空，所有 Promotion 仍被拒绝。
6. **诚实边界**：
   - 本地文件登记只提供**可审计的人工批准声明**：`approved_by` 是被声明的名字，**不**认证操作系统用户或任何身份，也不证明批准人
     具备权限；
   - 它**不是**生产 Control Plane，**不**授权实盘、资金或生产部署；ADR-0005 的生产部署审查与 D-01 / D-02 的 PostgreSQL Control Plane
     仍在范围外；
   - 登记不选择、不冻结任何 Profile 数值（D-09 TBD-1..5 仍由 Raphael 决定）；真实冻结记录只能按 Raphael 的冻结决定写入，
     测试只在临时目录中使用 TEST ONLY Profile 与报告；
   - **信任边界**：锚点与登记被一起回滚（一起截断或一起替换为更早的副本）仍不可检测。锚点必须放在登记写入者无法回滚的
     存储上；本批只要求锚点位于登记目录之外，不能证明两者不会被同一操作者一起回滚。

## 备选方案（Alternatives）

| 方案 | 为何未选 |
|---|---|
| Q1 = B：在 `StrategyRegistry` 日志中增加 `profile.frozen` 记录类型 | 把 Profile 冻结（验证架构状态）混入策略 Artifact 登记，每次策略登记重放都要承担 Profile 规则（Codex 选 A） |
| Q2 = B：要求校准报告的 `candidate_profiles` 含最终冻结的 Profile | 回填 `provenance` 必然改变 Profile 哈希，只能按 ref 比较——这是一条没有 ADR 依据的新规则（Codex 选 A） |
| 继续信任对象上的 `status = FROZEN` | 不受哈希或记录约束，调用方可任意声明 |
| 把 `status` 放回内容哈希 | 推翻 ADR-0008 决策 3，改变已发布契约的哈希；本 ADR 明确不改 ADR-0008 |

## 后果（Consequences）

- 正面：冻结成为可重放、可审计、只追加的事实；Promotion 不再接受调用方自报的冻结。
- 负面 / 代价：多一个需要运维锚点的登记目录；批准人身份仍只是声明。
- 需要迁移的内容：无（登记为空；已有 Profile / 报告 / Registry 不变）。
- 对复现性：不改任何已有哈希；登记记录自身带 `freeze_id` 与哈希链。

## 验证（实施必须有测试）

重放；部分尾行（崩溃残留）；锚点（缺失 / 位于目录内即拒绝打开、整行截断、锚点外多写、唯一崩溃窗口补写；测试用临时登记目录 +
同级临时锚点文件）；重复与同 ref 冲突；错 Profile（哈希 / ref / 非 FROZEN /
provenance 不符）与错校准证据（类型、自哈希、非规范字节、blob 缺失或篡改）；错 `format_version`；无批准人 / 非 UTC 时间；单写者锁；
Promotion 成功（完整 TEST ONLY 证据 + 有效登记）与 fail closed（`status = FROZEN` 但无登记、登记的是另一哈希 / 另一报告）。

## 合规检查

- [x] 不修改 Domain Contract、Schema、Profile 结构 / 数值 / 哈希（H1、H2、H3）
- [x] 不修改 Validation Constitution 或 ADR-0008
- [x] Domain 层无新依赖；`infrastructure/` 不 import `research/` / `apps/`
- [x] 不触网、不涉及实盘 / 资金 / 生产部署
- [x] Codex 最终复核（2026-09-27 接受；ADR / 实现的接受**不等于** Phase 4 或 Phase 5 验收：冻结登记仍为空（没有任何 `profile.frozen` 记录，所有 Promotion 仍以 `profile_not_frozen` 被拒），Validation Profile 数值（D-09 TBD-1..5）仍未冻结）

## 实施记录（B56，2026-09-27）

- `3e1dca8` 本 ADR；`6e482e2` `ProfileFreezeRegistry`；`a89b00a` 复核返修（写路径失败即作废实例、任何写入之前执行全部规则）；
  `7f93629` Promotion 以登记为权威冻结来源。状态 CODE_COMPLETE / DEBUG_PENDING；登记为空，所有晋升仍被拒。
- 详见 [ADR-0062 实施说明](../reviews/2026-09-27-adr-0062-implementation.md)（测试、变异、原样门禁结果与未完成边界）。

## 接受记录（2026-09-27）

- 接受者：Codex（依 Raphael 2026-09-23 授权）。依据：阶段 1（`6e482e2`，复核返修 `a89b00a`）与阶段 2（`7f93629`）代码复核通过；
  阶段 2 提交前扩大门禁 `pytest -m "not postgres" tests/promotion tests/research tests/apps tests/test_*.py` → 4615 passed（退出码 0），
  ruff / format / mypy 退出码 0；阶段 3 文档（`0cdf19f`、`3c344df`）检查通过。
- 范围：接受的是本 ADR 的架构与实现方案；ADR / 实现的接受**不等于** Phase 4 或 Phase 5 验收：冻结登记仍为空（没有任何 `profile.frozen` 记录，所有 Promotion 仍以 `profile_not_frozen` 被拒），Validation Profile 数值（D-09 TBD-1..5）仍未冻结；批准人只是声明、不认证身份；登记不是生产 Control Plane，不授权实盘 / 资金 / 部署。
