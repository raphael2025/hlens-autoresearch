# ADR-0071：P7 failed experiment round 的只读人工复核摘要

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | Codex，依 Raphael 对项目与技术决策的明确授权；经 Claude 与独立 Codex 子代理只读复核后接受 |
| 相关 Phase | Phase 7、Phase 11 loop host |
| 影响范围 | `research/loop` 只读投影；不修改 `core/`、LoopRecord、checkpoint、TrialLedger、生命周期契约或持久化 Schema |

## 背景

ADR-0070 要求对最后一轮 `experiment` stage 为 `FAILED` 的 loop 停止自动推进，并由人工检查 round record、lifecycle transitions、TrialLedger 和持久 checkpoint。当前 fail-stop 状态由最后一条已记录 `LoopRecord` 重建，但没有统一的数据视图供人工复核。

已审查的 `DurableState` 是 `open_state()` 在验证 audit、memory journal、各 append-only ledger、checkpoint 位置和可用 anchor 后返回的内存对象。`open_state()` 本身不是只读操作：它获取单写者锁、可能创建状态目录 / header，并观察 review queue；本决策不增加或包装该打开过程。

失败轮的持久证据并不包含所有执行信息。批次成功前 outcomes 不会写入 memory；experiment stage 的失败记录没有 summary；完整异常与 traceback 不持久化。因此摘要必须保留证据缺口，不能推断失败 trial、补全 outcome 或声称完整重建执行过程。

## 决策

1. 在 `research/loop` 提供一个纯内存、只读的 `FailedRoundReviewPacket` 构造函数。调用前置条件是：输入由 `open_state()` 当前成功返回，`state.audit.durable` 为真、`state.lock is not None and state.lock.held` 为真、没有未记录 open round，且调用发生于 ADR-0070 fail-stop 状态；调用方须保证构造期间不并发修改状态。前置条件不满足时抛出专用 review error。函数不打开目录、不重新验证或修复文件、不申请锁、不发布 anchor、不读取 `reports_root` / event bus / Provider，也不写任何状态。它不认证 `DurableState` 的构造来源。
2. 若最后一条 audit record 的 experiment stage 不为 `FAILED`，返回 `None`。对 failed record，按 `round_index` 必须唯一找到对应 `round_memory` checkpoint，并确认 `round_index` 与 `record_hash` 一致；不匹配、缺失、重复或有歧义时抛出专用 review error。不得用 memory journal 的 tip 当作 round checkpoint；末尾可有合法 `between_rounds` checkpoint。
3. 摘要只包含现存的直接证据：经重建并由 `open_state()` 交叉校验的 `LoopRecord.payload()` 与 record content hash、其中嵌入的 experiment stage / lifecycle transition payload、匹配的 `round_memory` AppendOnlyJournal entry（含 seq / type / payload / prev_hash / hash），以及 TrialLedger 原始 journal entries 的半开 seq 区间 `(previous_round_seq, failed_round_seq]`（round 0 起始 seq 为 0；对应 Python slice `entries[previous_round_seq:failed_round_seq]`）。区间端点必须与相邻 round checkpoint 的 `heads.trial_ledger` 对齐；缺少前后 checkpoint 或 ledger journal 时抛出专用 review error。端点相等且 ledger journal 存在时，空条目区间是有效证据。条目保留原始顺序及各自 hash-chain 元数据，不推断 ledger 条目与失败 trial 的一一对应关系。摘要不包含 audit journal envelope hash；`record_hash` 不是该 envelope hash。
4. 固定列出证据缺口：失败前未持久化的逐 trial outcome、精确失败 trial 序号、完整异常 / traceback、audit journal envelope hash，以及外部 LLM / Provider 内部状态。不得将其推断或补写成事实。明确呈现证据不对称：同轮 transitions / ledger 行可存在，而失败 experiment 的完整 outcomes 不存在。
5. packet 是研究侧临时只读视图，不新增 JSON Schema、API、UI、CLI、持久化报告、审计事件、生命周期变更或恢复入口。序列化字段与排序确定，便于调用方本地阅读；其内容不声称是经认证的“修复建议”或完整真相。
6. 不定义、执行或暗示任何自动 / 人工恢复动作；不重试、不补 outcomes、不回滚 journal、不推进生命周期、不修改 TrialLedger。不得调用 `open_state()` / `mkdir` / 加锁，不得调用 `MemoryCheckpoint.__call__` / `between_rounds`、`DurableState.after_approval` / `publish_anchor`、`ReviewQueue.approve` 或 `AppendOnlyJournal.append`。后续恢复协议仍须另立 ADR。

## 兼容性与边界

该功能只读取已经加载的 `DurableState` 内存对象，不改变其持久字节、record hash、checkpoint、fingerprint 或外部契约。`DurableState` 虽为 frozen dataclass，内部 audit 与 journal 仍是可变对象；调用方必须在 `open_state()` 当前持锁且 fail-stop 的状态下同步调用，并保证构造期间没有其他写者。本函数不证明对象由可信 opener 创建，不提供并发快照锁。若需要可独立启动的只读 opener、持久化 packet 或 API 暴露，须另立 ADR 并评估锚点、锁与信任边界。

## 实施状态

Codex 于 2026-09-27 依 Raphael 明确的项目与架构决策授权接受本 ADR。Claude、Cursor 与独立 Codex 子代理只读复核确认修改后的 checkpoint 对齐、ledger 区间、hash 语义与副作用边界可实现。实现限于新增 `research/loop/recovery_review.py` 与模块 README；目标文件 Ruff、format、mypy 和 `git diff --check` 通过，未运行测试 / build。实现完成仍不构成 Phase 7 / 11 验收。
