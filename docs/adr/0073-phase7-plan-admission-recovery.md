# ADR-0073：P7 typed-plan 预登记与崩溃恢复

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-28 |
| 决策者 | **Codex 依 Raphael 2026-09-28 项目全权委托接受设计** |
| 起草者 | Codex |
| 相关 Phase | Phase 7、Phase 11 loop host |
| 影响范围 | Research plan admission、TrialLedger、durable loop checkpoint / anchor；不修改 `core/`、契约、Schema、Constitution 或 Profile |

## 背景

[ADR-0068](0068-phase7-typed-operator-plans.md) 已接受 typed-plan 的闭世界 AST 与拒绝边界，但明确将执行计划的持久化位置及其与 TrialLedger 全量预登记之间的崩溃原子性留待后续决定。typed plan 仍全部 `NOT_RUNNABLE`。

目前 `TrialLedger.register_batch(hypotheses)` 已把全部新登记项作为一个 `register_batch` journal event 追加，并在 append 成功后更新内存状态。该保证只覆盖一个 TrialLedger event：它不与 plan journal、Research Loop audit、memory checkpoint 或 external anchor 构成跨文件事务。各 AppendOnlyJournal 独立校验 hash chain，追加会 `flush` / `fsync`；不完整尾行或链损坏会被拒绝，不自动修复。

durable loop 以 `audit.jsonl` 记录 round，以 `memory.jsonl` checkpoint 记录各 journal 的 seq / hash，并可由外部 `StateAnchor` 检测整目录回滚。新 journal 若不进入 checkpoint 与 anchor，重开时不能区分预期的 admission 写入、未 checkpoint 的 ledger 尾部和历史损坏。

[ADR-0070](0070-p7-partial-experiment-fail-stop.md) 已要求：experiment round 执行失败后停止并人工审查；未记录的 started round 也必须 fail closed。它不允许重开后自动重跑该 round。

## 决定（提案）

### 1. Admission 顺序与执行门

每个 typed-plan admission 必须在且仅在其对应 round 的 `LoopAuditLog.begin_round` 已持久追加并 fsync 后进行。Admission 记录绑定 `loop_id`、round index 及该 round 的持久 started audit entry 身份。只有 audit 中存在**唯一、匹配当前 admission round 且已持久 begin** 的 open round 时才可 admission；round 未开始、open round 缺失 / 多个 / 身份不匹配，或 loop 已处于 `recovery_required` 时一律拒绝。

一次 admission 的持久顺序固定为：

1. 在 `plan_admission.jsonl` 追加并 fsync `PREPARE`。该记录包括 transaction id、round 身份、完整规范化 typed-plan 与 plan hash、compiler / operator / provider 精确身份与内容 hash、解析输入和 hash、lowered outputs / ExperimentSpec 与 hash、确定性排序的全量 Hypothesis payload / hash、TrialLedger 当前基线位置，以及预期的 `register_batch` payload / hash。
2. 在 TrialLedger journal 以一个 `register_batch` event 登记该事务的全部新 Hypothesis。单 event 是批次登记的 replay 单位；禁止逐 Hypothesis append。保留 TrialLedger 现有“先校验、event append 成功后才更新内存”的行为。
3. 在 `plan_admission.jsonl` 追加并 fsync `COMMIT`，引用 PREPARE 的 seq / hash 及精确的 TrialLedger batch event seq / hash。
4. 在 `memory.jsonl` 追加并 fsync 一个 `plan_admission` checkpoint，绑定 round started entry、transaction / COMMIT 身份，以及此时 `plan_admission.jsonl` 和 TrialLedger 的位置；随后推进 external anchor。

Research Loop 只能把存在有效 `COMMIT` 且 ledger event 完全匹配的 plan 交给执行路径。执行结果仍由 LoopAudit / round checkpoint 记录；不能仅凭 PREPARE、Hypothesis 文本、TrialLedger 登记或 plan hash 开始执行。

单写者 loop state lock 必须覆盖 PREPARE、TrialLedger event、COMMIT、memory checkpoint 和 anchor 更新。每个跨文件步骤仍可被进程中断；协议依靠已 fsync 的前置日志和精确重放处理这些中断，不声称多个文件的底层写入同时原子。

### 2. Authority 与记录职责

| 记录 | 权威职责 | 不代表什么 |
|---|---|---|
| `plan_admission.jsonl` PREPARE / COMMIT | Admission 的完整意图、plan / implementation 身份、transaction 状态及 ledger event 的绑定 | 不证明 experiment 已执行或成功 |
| `trial_ledger.jsonl` | 已登记 Hypothesis、trial 顺序、族计数及实际 batch event | 不单独证明该 batch 属于哪个可执行 plan，也不表示 outcome 完成 |
| `audit.jsonl` | Round started / recorded 及 experiment 执行结果、失败状态 | 不取代 plan manifest 或 ledger 明细 |
| `memory.jsonl` checkpoint | round / plan-admission 边界及当时各持久文件位置 | 不取代任何源 journal 的内容 |
| external anchor | 检测 checkpoint 和日志相对于已发布目录 head 的回滚 / 分叉 | 不补造缺少的日志记录 |

各 journal 必须分别通过 hash-chain 与严格 payload replay；跨记录一致性是执行准入条件。任何一个 authority 缺失或相互矛盾都不能由其他文件推断补足。

### 3. Identity reuse 与 trial 计数

1. PREPARE 中的 batch 固定包含排序稳定且身份唯一的 Hypothesis 集合。执行用 admission 要求集合中的每个 Hypothesis 在 PREPARE 的 TrialLedger 基线时均为新身份；`name@version` 内容冲突一律在写 PREPARE 前拒绝。
2. 若身份已登记且内容完全相同，现有 `register_batch` 将其视为幂等重复，不追加 entry，也不增加 trial count。该结果不能当作一个新 trial，更不能再次执行同一 admission。Typed-plan 的正常恢复必须通过原 transaction 的 PREPARE / batch event / COMMIT 对账，而不是把它当新批次重新登记。
3. 对已登记 Hypothesis 的新评估必须有显式且唯一的 attempt identity，并作为一项新 trial 预登记。当前 `register_batch` event 不承载 re-evaluation attempt；在另一个明确决议与单 event replay 格式支持它之前，含 identity reuse / reevaluation 的 typed-plan admission 必须拒绝，不得以幂等重复冒充新 trial。
4. 每个首次执行的派生 experiment 恰有一个此前未登记的 trial。执行重试不得复用原 trial；必须走未来批准的显式新 attempt 机制。失败 trial 不删除、不回滚、不从多重比较计数中排除。

### 4. 精确恢复状态机

重开必须先获取 state lock，读取并验证 audit、plan admission、TrialLedger、memory checkpoint 和 anchor 的 journal envelope。只允许以下确定性恢复，不运行 Provider、compiler 或 experiment：

| 重开时证据 | 允许动作 |
|---|---|
| 无 PREPARE，且没有任何未解释的 `register_batch` 尾记录或 plan-admission checkpoint | 无 admission 需要恢复；继续常规验证。任何孤儿 batch event、孤儿 plan checkpoint，或无法由其他已提交 PREPARE 精确解释的尾记录均 fail closed |
| 有完整 PREPARE，无 COMMIT；TrialLedger 仍精确位于 PREPARE 的基线 seq / hash | 重新校验 PREPARE 中冻结的 payload，追加原 batch 的唯一 `register_batch` event，再追加 COMMIT 与 admission checkpoint |
| 有完整 PREPARE，无 COMMIT；TrialLedger 紧接基线已有一个 event，且 type、完整 payload、seq、prev_hash、event hash 与 PREPARE 精确匹配 | 不再登记；追加 COMMIT 与 admission checkpoint |
| 有 PREPARE 与 COMMIT，ledger event 精确匹配，但 admission checkpoint 缺失且所有可解释文件尾部都仅对应该事务 | 补写 admission checkpoint，并按已验证 head 更新 anchor |
| 有完整 COMMIT 与 checkpoint，所有文件位置及 hashes 一致 | 不做事务修复；进入后续 loop 状态检查 |

恢复对账必须可幂等重入。PREPARE 绑定的 TrialLedger 基线只允许存在其声明的那个 batch event；从 PREPARE 到该 event 之间不允许其他 ledger 写入。payload 不能由当前代码重新生成或“等价”替换，必须按持久化字节语义和 hash 精确比较。

以下情况一律拒绝启动 / 执行并保留原始证据：PREPARE 缺字段、重复或 hash 不符；journal 坏链、partial trailing line 或未知 event；PREPARE 后 ledger 有缺失、额外、乱序、冲突或非唯一 event；COMMIT 没有唯一 PREPARE / ledger event；已 checkpoint 的 seq / hash 回退或不符；checkpoint 未涵盖的额外日志无法由唯一 PREPARE 解释；external anchor 与已验证历史分叉；或同一个 transaction id 被赋予不同 payload。不得删行、改写旧 checkpoint、回退 trial 计数、跳过冲突或自动生成新的 attempt。

### 5. Loop checkpoint、anchor 与崩溃边界

1. 新增 `plan_admission.jsonl` 到 durable state 的 journal 集合；memory checkpoint 的 `heads` 必须在每个 `round_memory`、既有 `between_rounds` 和新的 `plan_admission` checkpoint 中记录它及 TrialLedger 的 seq / hash。每条 `plan_admission` checkpoint 必须引用精确的 round started entry、PREPARE / COMMIT 和 ledger event，且 round index 与当前 audit open round 一致。
2. External `StateHead.heads` 同步包含 `plan_admission.jsonl`。Admission checkpoint fsync 后推进 anchor；重开若发现 anchor 落后但后续历史逐行完全一致，可在所有校验通过后单向前移；anchor 超前、分叉或对应日志缺失则拒绝。
3. 重开处理事务恢复必须先于通用 checkpoint-position 与“存在未记录 started round”拒绝，以便对账已经持久开始 round 内发生的纯登记写入。恢复仅能追加缺失的 batch / COMMIT / checkpoint / anchor，不运行 experiment、不写 outcome、不补 lifecycle transition。
4. 完成纯登记对账后，若 audit 仍有未记录 open round，按 durable loop 原规则拒绝继续；若最后记录的 experiment stage 为 `FAILED`，按 ADR-0070 设置 `recovery_required` 并拒绝任何后续调度。人工 review packet 只提供证据视图；它不授权重试、补 outcome 或清除此状态。
5. 若 round 已成功记录，round memory checkpoint 必须再次覆盖该轮所有 plan admission、TrialLedger、audit 与相关日志的最终 head。Round outcome 和 plan admission 若不能由当前 LoopRecord / checkpoint 清晰关联，实现前须扩展 research audit summary；不得只靠日志文件名或当前 head 推断归属。

### 6. Durable state version 与旧目录兼容

新增 journal head 会改变 `memory.jsonl` checkpoint 形状，因此新增持久格式采用 `STATE_VERSION = 4`。不得原地改写、回填、删除或自动迁移现有 v3 state directory，也不得把 v3 checkpoint 伪装成 v4。

实现必须保留明确的版本分支：已有 v3 目录可继续按其原 v3 形状执行不含 typed-plan admission 的既有 loop 行为，不能写 v4 专属 checkpoint 字段；v3 state directory 上一律禁止 typed-plan admission。启用 admission 的 loop 必须使用以 v4 header 创建的新 state directory / loop identity。未知版本、v3 目录中出现 v4 专属日志或 checkpoint、或 v4 目录缺少必需 plan journal header 时 fail closed。迁移工具若未来需要，另立决议；本 ADR 不批准迁移或删除。

若实现评估发现保留 v3 写入路径不可安全维护，应在编码前提出替代兼容方案并另行接受，不能静默破坏现有 state。

### 7. 明确不在本提案授权内的行为

- 不定义或启用 `conditioning`、`interaction`、`temporal`、`transformation`、`ensemble`、`negation` 的执行语义；六类 operator 仍 `NOT_RUNNABLE`，allowlist 保持空，除非各自另有逐算子决议。
- 不修改 contracts、`core/`、Schema、Constitution、生命周期、Validation Profile 或 P4 / P8 判定。
- 不解 D-LIST / ADR-0051，不接入或推断历史 listing 数据。
- 不启用 live trading，不扩大 API 写权限，不自动恢复 experiment outcomes，不回滚或重试失败 round。
- 不把本 ADR 的 Proposed 状态、文档完成或后续代码提交表述为 CODE_COMPLETE、Phase 验收或 Research 结果。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 只在 `register_batch` 里登记，不写 plan manifest | 改动少、一个 ledger event 自身可 replay | 无法证明批次对应哪个 plan / 实现，也无法判断跨 journal 中断点 | 不足以满足 ADR-0068 的持久计划审计和精确关联 |
| 先写 ledger，再补 plan audit | 先完成计数 | crash 可留下无法归属的 ledger event；启动时不能安全判断它是否来自该 plan | 不可恢复且不能猜测 |
| 把 audit、ledger、checkpoint 合成一个文件 | 单文件事务更直接 | 重构既有持久格式与 authority 边界，影响面大 | 本提案保留已有 journal 和 checkpoint 架构，以 PREPARE / COMMIT 协调 |
| 发现任何中断就人工处理，不进行确定性登记对账 | 恢复逻辑最少 | PREPARE 与 ledger 的可判定安全前缀也无法一致收敛，需手工改持久证据 | 对完全匹配且尚未执行的登记事务，可安全追加缺失日志；experiment round 仍依 ADR-0070 fail-stop |

## 后果（Consequences）

- 正面：计划意图、trial 登记和执行 round 各有可核验 authority；进程在三个 journal 步骤之间退出时，只按精确持久证据收敛，不重复计数或运行。
- 代价：新增 plan admission journal、checkpoint 类型、state format version 和 opener recovery 顺序；需维护 v3 / v4 格式分支，并为文件锁、anchor、checkpoint、各种 crash point 做后续验收。
- 限制：单个 `register_batch` event 避免批次合法前缀，但 JSONL 底层损坏仍整体拒绝。两阶段日志不能让跨文件写同时原子，也不能恢复已经开始或失败的 experiment round。
- trial 语义：相同身份的幂等重用不会新增 trial，不能据此再次执行。真正的新 attempt 需要显式登记并计数；当前 batch event 尚不支持批量 reevaluation attempt，相关能力保持 fail closed。
- 兼容性：既有 v3 state 不迁移或重写；仅新 v4 state 可用 plan admission。具体实现不得降低旧 state 的历史验证强度。

## 实施前置条件与未决项

本 ADR 若被接受，只表示批准该恢复协议设计，不批准实现或 operator runnable。开始实现前至少须将以下项目落实为可审查的代码方案：

1. `plan_admission.jsonl` 的固定 payload schema、transaction id 派生方式、严格 reducer / 重放状态机，以及同一 Hypothesis 内容编码的复用方式。
2. `AppendOnlyJournal` / `TrialLedger` 的 recovery API 如何在一个已持有 loop state lock 的 opener 内实现精确“追加缺失 event”，避免绕过 ledger 内存状态、ledger mutex 与 stale journal 检查。
3. `LoopAuditLog` 如何暴露并 hash-bind open round 的 started entry，以供 PREPARE 和 `plan_admission` checkpoint 精确引用。
4. `MemoryCheckpoint` 对 `plan_admission` line 的顺序验证、跨 journal positions、anchor 同步和 v3 / v4 双格式 opener 的兼容细节。
5. plan lowering / batch 是否将每个 Hypothesis 映射为恰好一个新 trial，及拒绝复用 identity 的审计编码。不得在计数含糊时启用执行。
6. 计划拒绝、PREPARE 写入前错误和执行失败分别由哪个持久记录承载；须与 ADR-0068 §3 的拒绝审计要求一致，但不得把异常 / 不存在的 outcome 伪装成可恢复结果。

若实现需要修改 `core/`、冻结契约、Schema、Constitution、Profile 或 D-LIST 决策，必须另行提交相应 ADR / 明确授权，不能由本 ADR 推出。

## 验收边界

只有在后续实现完成并由 Raphael 指定的验收流程验证之后，才能报告 CODE_COMPLETE 或接受 Phase。验收至少覆盖：PREPARE 后中断、batch event 后中断、COMMIT 后 checkpoint 前中断、checkpoint 后 anchor 前中断、精确恢复的重复调用、坏链 / 尾行 / 多余 ledger event 拒绝、identity reuse 不增加 trial、open round 与 failed experiment round 都不自动重跑，以及 v3 / v4 state 格式兼容。此处列出验收要求不代表本任务运行了任何检查。

## 参考

- [ADR-0068](0068-phase7-typed-operator-plans.md)：typed-plan 闭世界边界、审计字段和算子 fail-closed 要求。
- [ADR-0070](0070-p7-partial-experiment-fail-stop.md)：部分 experiment stage 失败后必须 fail-stop 并人工审查。
- [ADR-0071](0071-p7-failed-round-review-packet.md)：失败 round 的只读人工复核摘要，不提供恢复授权。
- [ADR-0049](0049-continuous-research-loop.md)：durable Research Loop、checkpoint 与继续规则。
- `research/hypotheses/ledger.py`：`TrialLedger.register_batch` 的单 event 登记与精确重放。
- `research/loop/durable.py`：durable state journals、memory checkpoint、STATE_VERSION、external anchor 与 opener cross-check。
- `apps/worker/loop.py`：LoopAudit、started / recorded round 与 recovery-required fail-stop。

## 接受记录（2026-09-28）

Codex 依 Raphael 对项目决策与开发的全权委托接受本 ADR 的 PREPARE → 单一 TrialLedger `register_batch` event → COMMIT 协议、精确恢复规则、checkpoint / anchor 接线与 v3 / v4 兼容边界。此接受只批准设计，**不代表实现已开始或完成**，也不启用任何 typed-plan operator。实现前必须逐项解决上文所列 payload / reducer、ledger recovery API、open-round identity、checkpoint / anchor 和拒绝审计细节；任何 contract、Schema、`core/`、Constitution、Profile 或 D-LIST 变化须另行决议。验收仍按 Raphael 后续指定流程进行。
