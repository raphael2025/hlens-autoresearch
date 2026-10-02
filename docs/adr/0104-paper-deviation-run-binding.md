# ADR-0104: 纸面偏差的运行绑定与兼容规则

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权）；修订 ADR-0079 |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」）；同时重新确认 ADR-0079 |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 10（Dynamic Strategy Router，纸面） |
| 影响范围 | `research/router/deviation.py`、`research/reports/deviation.py`、`apps/api/report_dto.py`、`apps/web`（deviation DTO）；不改 `core/` |
| 是否破坏兼容 | 否（新写入为 payload 2.1.0 / scope 1.1.0；旧报告原样可读） |

## 背景（Context）

ADR-0079 把 paper deviation 绑定到 `ValidationProfile.scope` 四字段与 Profile / Report 哈希（payload 2.0.0、scope 1.0.0）。它没有规定 deviation 与具体 Router 配置、实验、价格数据、成本模型、时间窗口之间的兼容关系，导致：同名同版本但不同 `RouterSpec` 的运行可借用他人的 PASS 报告；参照回测可用不同 bars；cost_model 不同的比较被默认放行；报告与运行之间没有硬绑定；全库没有消费侧校验调用方。

## 决策（Decision）

1. **运行绑定**：`DeclaredScopeIdentity` 增加 `run_binding: RunBinding`（scope `1.1.0`，payload `2.1.0`），字段：`router_spec_hash`、`router_strategy_spec_hash`、`experiment_hash`（取自 report）、`bars_hash = content_hash(run.request.bars)`、`window_start` / `window_end`（run bars 的最小 / 最大 interval）、`cost_model_hash`、`reference_request_hash`。`scope_hash` 覆盖全部字段。
2. **兼容规则**：`paper_deviation` 新增必填 `router_validation: RouterValidation`，以其 `binding_hash` 证明 report 与 `router_spec_hash` 一致（不要求 `paper_run_hash` 相等）。`reference_request` 改为必填。拒绝码（`DeviationError.code`，消息兼容）：
   - 沿用：`scope_evidence_missing`、`subject_mismatch`、`profile_mismatch`、`verdict_not_pass`、`sealed_oos_missing`、`symbol_mismatch`；
   - 新增：`router_spec_mismatch`、`experiment_mismatch`、`reference_request_missing`、`bars_mismatch`、`cost_model_mismatch`、`scope_version_unsupported`。
   - cost_model 不同**一律拒绝**，不提供放行开关。
   - 多标的 Router 继续拒绝（保持 ADR-0079 §4）。
   - 时间窗口记录在 `run_binding` 中，不作拒绝条件（report 不携带可核对的 OOS 窗口）。
3. **版本规则**：同 major 内读取方支持所需最低 minor；`validate_scope_bound_payload` 增加 `required_min_scope_version`（默认 `1.0.0` 保持现状）；做"可比"判断的消费者传 `1.1.0`；不同 major 一律拒绝。
4. **旧报告（H6）**：payload 1.0.0 / 2.0.0 原样保留、原样可读，标记为"scope-only，无运行绑定"，不得作为可比证据；不回填、不迁移。
5. **展示**：API DTO 支持 `{1.0.0, 2.0.0, 2.1.0}`；Web 区分"scope-only（legacy）"与"带运行绑定"。
6. 显式 dataset 快照声明（`DatasetRef`，scope 2.0.0）不在本 ADR 范围，需要时另立。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 同时要求显式 `dataset_snapshots` | 直接交叉核对数据版本 | 接口更重；`PriceBar` 无数据集字段，仍只是声明 | 留作 scope 2.0.0 |
| cost_model 不同可显式放行 | 灵活 | 打开"挑成本模型"的口子（H3 精神） | 一律拒绝更安全 |

## 后果（Consequences）

- 正面：deviation 只能与同一 Router 配置、同一实验、同一价格数据、同一成本模型的参照比较。
- 负面 / 代价：现有测试夹具（如 `experiment_hash="a"*64`）须改为真实绑定；约 120 行代码、200 行测试。
- 对复现性的影响：旧报告哈希不变；新报告为 2.1.0。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变
