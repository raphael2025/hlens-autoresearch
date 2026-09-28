# ADR-0083：P7 failed round 的 durable 人工重试 admission

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-28 |
| 决策者 | Codex 依 Raphael 2026-09-28 授权决定 |
| 相关 Phase | Phase 7 — Discovery；Phase 11 loop host |
| 影响范围 | `research/loop`、`apps/worker/loop.py`；不改 `core/`、Constitution、Validation Profile 或阈值 |
| 兼容性 | 新建 v6 retry state；v3/v4/v5 持久字节与读取行为不变；`TypedPlan.runnable` 仍为 `False` |

## 背景与决定

[ADR-0070](0070-p7-partial-experiment-fail-stop.md) 在 experiment stage 失败后 fail-stop；[ADR-0071](0071-p7-failed-round-review-packet.md) 只提供只读 packet，不授予恢复权；[ADR-0073](0073-phase7-plan-admission-recovery.md) 的 v4 PREPARE / TrialLedger / COMMIT / checkpoint / anchor 事务只用于 typed-plan；[ADR-0074](0074-p7-bounded-operator.md) 的 v5 绑定 operator identity。重试必须成为新 attempt，旧失败证据及其 ledger 记录永久保留。

批准一个**显式、人工发起、不开启调度**的 retry admission。协议只接受一份当前打开 durable state 上重建的 ADR-0071 packet、非空人工 reviewer 声明和人工给出的有序 retry manifest。manifest 每项是已登记 hypothesis 的精确 `name@version`、其当前内容 hash、以及全新的 attempt key。packet 不推断失败 trial 与 hypothesis 的映射；调用者必须逐项明确选择。相同 hypothesis 可多次列出，但 attempt key 全局不得在本 ledger 中复用，manifest 内不得重复。

重试使用新的 **durable state version 6**。v6 header / memory journal 的新增 retry event 使用严格固定字段；v3/v4/v5 的 header、checkpoint、anchor 与 reducer 分支保持原样。v6 必须有 ADR-0073 plan-admission journal（仍沿用 v4 语义）以及 retry journal；两者均由 `loop_id` 和状态版本绑定。retry journal 是独立 hash-chain，事件只有 `retry_prepare` 与 `retry_commit`。PREPARE 保存完整 canonical packet payload/hash、失败 `record_hash`、reviewer、manifest、ledger baseline `(seq, hash)` 和 retry id。COMMIT 绑定 PREPARE seq/hash、每个新 `reevaluate` ledger entry 的 seq/hash、最终 ledger head 与 memory checkpoint 的 seq。memory checkpoint 的 `retry_admission` 行记录 retry id、COMMIT 位置和完整 file heads；该 checkpoint 自身的 hash 由后续 external anchor 绑定。外部 anchor 仅在 checkpoint fsync 后推进，head 中包含 retry journal 位置。审阅者身份是调用者声明，不是认证凭据。

## 写入顺序与崩溃恢复

所有写入在 `open_state()` 成功、单写者 state lock 与 admission gate 均持有、无 open round / pending plan admission、anchor 精确等于当前 head 后进行：

1. 校验最终记录为 experiment `FAILED`，在同一锁定视图重建 ADR-0071 packet 并要求传入 packet canonical bytes/hash 完全一致；核对 review journal、audit、memory checkpoints、TrialLedger 与 anchor。
2. 校验 reviewer 为非空且非自动化 loop / system identity；manifest 非空、引用的 hypothesis 已登记且内容 hash 相同；attempt key 非空、规范化后唯一，且 ledger 未使用。预算绑定当前 loop fingerprint / `LoopBudget`，不增加、不清零。拒绝重试预算会超出剩余硬上限的请求。
3. fsync `retry_prepare`。此后 admission gate 封闭普通 ledger writes；按 manifest 顺序逐项追加标准 TrialLedger `reevaluate` 事件。每项都是新 trial，因此 family trial count 与多重检验计数单调增加。
4. fsync `retry_commit`，再写 memory `retry_admission` checkpoint，最后推进 anchor。只有此顺序全部完成后，显式入口才解除当前 `ResearchLoop` 的 ADR-0070 fail-stop 并允许新 round；它不会运行实验。下一 round 使用新的 round index 和已登记 attempt，不重放失败 round。

v6 opener 先验证全部链、packet/hash、失败记录、manifest、ledger baseline / 尾部、checkpoint 与 anchor prefix，再做恢复。PREPARE 后崩溃時，只接受 ledger 在 baseline 到 manifest 的精确連續 `reevaluate` 前綴；驗證每一行都與 PREPARE 綁定 hypothesis / attempt 完全一致，補寫唯一缺少的 suffix，然後補 COMMIT、checkpoint、anchor。若 ledger 多出、缺中間行、attempt 或內容不符，或 PREPARE/COMMIT/checkpoint/anchor 出現分叉、重複/非尾部 pending、packet 已非最後失敗輪，拒絕開啟並保留所有資料；不得執行 Provider / stage。COMMIT 已存在時只可補 checkpoint 與 anchor；checkpoint 已存在時只可補 anchor。任何已完成 retry admission 不得再次消費。

## 拒絕條件與兼容

無 reviewer / packet、packet 過期或不匹配、manifest 空/重複/引用不匹配、attempt 重用、v3/v4/v5 state、lock/gate 缺失、非 experiment failure、open round、任何 ledger / journal / checkpoint / anchor 不一致、預算不足、或無法唯一確定恢復步驟，均 fail closed，且不得開始 experiment。失敗 audit record、TrialLedger 與 lifecycle history 不刪、不覆蓋、不隱藏；舊失敗 trial 永遠計數。retry 只新增 re-evaluation trial，不改 Constitution trial-count 規則。沒有 ADR-0071 review 即沒有 admission；不自動 retry、不接 scheduler、不啟用 P7 operator。v3/v4/v5 opener 仍只接受原版本與原事件集合，不遷移、不轉寫。

## 後果

Review packet 繼續是只讀觀測；只有 v6 的專用 admission 才能解除 loop instance 的 fail-stop。人工身份是可稽核聲明，非身份認證。v6 與舊版本不可互開；需要切換 loop identity / budget 時建立新 loop。實現不改 Domain Contract、Validation、Constitution 或 `TypedPlan.runnable`。

## 參考

- [ADR-0049](0049-continuous-research-loop.md)、[ADR-0070](0070-p7-partial-experiment-fail-stop.md)、[ADR-0071](0071-p7-failed-round-review-packet.md)、[ADR-0073](0073-phase7-plan-admission-recovery.md)、[ADR-0074](0074-p7-bounded-operator.md)
- `research/loop/durable.py`、`research/loop/recovery_review.py`、`apps/worker/loop.py`、`research/hypotheses/ledger.py`

## Amendment 1 — checkpoint hash binding

状态：**Accepted**（Codex 依 Raphael 2026-09-28 授权决定）

原决定要求 retry COMMIT 同时绑定 `retry_admission` memory checkpoint 的 seq/hash，而该 checkpoint 又必须绑定 COMMIT 的 seq/hash。两者的 hash 互相依赖，无法按单向 append-only 写入顺序构造。现改为 COMMIT 绑定预期 checkpoint seq；checkpoint 绑定精确 COMMIT seq/hash 和完整 journal heads；checkpoint hash 再由外部 anchor 的 state head 绑定。PREPARE → ledger suffix → COMMIT → checkpoint → anchor 顺序及其余约束不变。

## Amendment 2 — 一次失败一个 retry journal（G3）

状态：**Accepted**（Claude-PM 依 Raphael 授权于 2026-09-28 决定）

一个 retry journal 文件只承载**一次** retry admission：路径为 `<state_dir>/retry_admission/<失败记录 record_hash>.jsonl`，文件名与其中每个事件（PREPARE / COMMIT 的 `failed_record_hash`）共同绑定被重试的失败 audit 记录；每个文件至多一对 `retry_prepare` → `retry_commit`，已有事件的文件不得再次 PREPARE。重试再次失败会产生新的失败记录与新的 ADR-0071 review packet，因此对应新的 journal 文件；旧文件、旧失败记录与旧 ledger 行永久保留。memory checkpoint 的 `heads.retry_admission` 为全部非空 retry journal 位置 `{failed_record_hash, seq, hash}` 按 hash 排序的列表；`retry_admission` memory 行另记 `failed_record_hash`。retry 目录中任何其他文件、子目录或符号链接均 fail closed。本修订不改变 PREPARE → ledger suffix → COMMIT → checkpoint → anchor 顺序与其余约束。

## PM 决定（2026-09-28，Claude PM 依 Raphael 授权）：实施后遗留的 7 项

1. **重试专用轮次跳过演化阶段**：这类轮次只运行被准入的试验，不注册演化后代，避免超出预算而停机。由后续提交实现。
2. **已准入但未运行的试验**：因预算停机而没有运行的试验，仍计入 TrialLedger。多计不漏计，对多重检验更保守；这一行为明确接受。
3. **attempt key 保留前缀**：`loop_round:` 保留给循环自身的再评估，重试的 attempt key 不得使用。接受。
4. **同一轮内同一 hypothesis 出现两次**：同一次准入中不得重复出现同一 hypothesis，验证阶段按 `#attempt` 区分结果。防护与测试由后续提交补齐。
5. **恢复后先拒绝、由调用方显式重开**：接受，这样恢复与继续运行是两个可审计的步骤。
6. **审阅人身份**：作为可审计的声明加自动化身份黑名单处理，本 ADR 不引入认证。
7. **ADR-0074 operator 遇到 v6 目录**：按未知状态 fail closed。接受，operator 不处理重试。
