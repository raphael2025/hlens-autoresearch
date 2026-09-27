# ADR-0044: EventBusAdapter 契约、内存总线与 worker 幂等任务（Phase 11 地基）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 11（地基；完整持续研究循环待 P7 / P8 框架后接入） |
| 影响范围 | Contract（`core/contracts/event_bus.py`，additive，Schema 87 → 88）、`infrastructure/event_bus/`、`apps/worker/` |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `EventBusAdapter`：`publish` / `poll(consumer, topic, limit)` / `ack`；**至少一次**投递（未确认即重投），同主题按发布顺序；
   `BusMessage.message_id` = 主题、键与规范化载荷的内容哈希（伪造 ID 被拒绝）。contract suite 覆盖至少一次、消费者相互
   独立与主题内顺序（10-migration.md §2 的"至少一次投递 + 幂等消费测试"）。
2. 首个实现 `InMemoryEventBus`（进程内）；Phase 1～6 不引入 NATS（ADR-0021），外部总线以后按同一 Protocol 与 suite 接入。
3. `apps/worker` 的 `JobRunner`：任务身份 = 名称 + 参数的内容哈希（重复提交只运行一次）；处理失败按 `max_attempts` 重试后记为
   失败（不丢弃）；结果记录后才确认消息，崩溃重投由幂等吸收。worker 不 import `research/`。

## 后果

- 正面：P11 的调度、P7 的批量实验与 P13 的模拟执行可以共用同一总线与任务模型。
- 负面：内存总线不持久；持久化 / 跨进程需要外部总线（另立 ADR，涉及系统软件安装时须 Raphael 授权）。

## Implementation note (file-backed bus, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；`core/contracts/event_bus.py`（冻结）、Schema、
生命周期、Constitution、Profile 均不变；不安装任何软件。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
调试待办 C 节 P11（「总线只在内存中」）。

1. **`FileEventBus(root)`**（`infrastructure/event_bus/file.py`）：第二个 `EventBusAdapter` 实现，通过同一 provider-agnostic
   suite（`tests/contract_suites/event_bus.py`，新建目录与重新打开的目录各跑一遍）。目录布局：`bus.json`（格式 + `schema_version`
   1.0.0）、`topics/<topic>.jsonl`（每主题一个哈希链只追加日志，每次 `publish` 一行，append / flush / fsync）、
   `consumers/<sha256(consumer, topic)>.json`（每个（消费者，主题）的状态：已确认 ID 集、**offset** = 低水位（其前全部已确认，
   `poll` 从此开始）、写入时该主题日志的长度与头哈希、覆盖全部字段的 `state_hash`；临时文件 + fsync + `os.replace` + 目录 fsync
   原子替换）。返回的 `publish` / `ack` 均已落盘；`poll` 与 `ack` 之间崩溃 → 重开后重投（至少一次）。
2. **语义与 `InMemoryEventBus` 完全一致**（差分测试：同一随机操作序列、中途多次重开，逐次比较 `poll`）：`publish` 只追加，
   同一内容发布两次就有两条——Protocol 与内存总线都**没有**发布端按 `message_id` 去重的语义，所以本实现也不去重，
   去重仍由消费者按 `message_id` 完成；确认按 `message_id`，此后同一 ID 的后续副本也不再投给该消费者；确认尚未发布的 ID 被接受。
   唯一有意的差异：`ack` 的 ID 不是内容哈希（64 位小写十六进制）时拒绝（`ValueError`），因为它不可能指向任何消息。
   返回的消息是发布内容的 JSON 形式（`message_id` 本就由该形式计算，身份不变），重启前后相同。
3. **fail closed**：打开时重放并校验全部内容，任何不一致即 `BusCorrupted`，从不跳过或修复——篡改 / 重排 / 半行的日志行、
   断链、不是该主题合法 `BusMessage` 的记录（`message_id` 在 `BusMessage` 构造时复算）、被编辑的消费者状态、文件名与其
   （消费者，主题）不符、状态记录的日志长度 / 头哈希与现日志不符（尾部整行被删或换了日志）、offset 与日志 + 确认集不符、
   总线目录中的未知文件、非空且不是总线的目录、错误的 `bus.json`。中断的原子写留下的 `*.tmp` 被忽略（被替换的旧状态完整）。
   诚实边界：最后一次写消费者状态**之后**追加、又被从尾部整行删除的消息无法发现（没有锚点见过它们）。
4. **单写者**：构造时对 `root/.lock` 取非阻塞独占 `fcntl.flock`，被占用即 `BusLocked`（同进程或其他进程）；`close()` 或进程退出
   （内核释放 flock）即释放，崩溃不会留下陈旧锁。仅 POSIX。并发写者与跨进程读者不在范围内。
5. **为什么日志在 `infrastructure/event_bus/journal.py` 独立实现**：这是同一磁盘契约（行格式、GENESIS、哈希规则、fsync、
   拒绝规则）的第三个独立实现，与 `research.persistence` 和 `apps/worker/journal.py` 并列；测试要求它与 worker 的实现写出逐字节
   相同、可互相重放的文件。`infrastructure/` 不得 import `research/`（H5，研究代码不是生产代码）或 `apps/`（依赖方向
   `apps → application → domain ← infrastructure`）；`core` 的 Domain 不做 I/O，把存储 helper 放进 core 会改动冻结包（H1）。
   测试静态检查本包不 import `research` / `apps` / `plugins`。
6. **接线**：组合根不变——`research/loop/compose.py` 的 `open_synthetic_loop(..., bus=...)` 本来就由调用方注入总线，
   调用方可传 `FileEventBus(state_dir / "bus")`。测试证明这样重启后的轮次与内存总线、不中断运行完全相同（总线在所有哈希记录之外，
   记录哈希不变），且总线重放每轮的 `record_hash`。组合根在持久模式下**自动**使用 `state_dir/bus`、并把总线与审计交叉校验，
   是后续工作（未做）。
7. **不改变 D-10**（ADR-0021）：这是在无 NATS 期间的本地持久选项，不是 NATS 引入门的决定；外部总线仍按同一 Protocol 与 suite 接入，
   安装任何系统软件仍须 Raphael 授权（H12）。

## Implementation note (durable jobs and bus wiring, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；`core/contracts`（冻结）、Schema、生命周期、Constitution、
Profile 均不变；不安装任何软件。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。调试待办 C 节 P11（`JobRunner` 结果只在内存；组合根未自动
使用并核对 `state_dir/bus`，见上一条第 6 点）。

1. **持久任务结果（可选）**：`JobRunner(..., results=<path>, idempotent=<处理器名>)`。结果日志复用 `apps/worker/journal.py`（与审计同一哈希链
   磁盘契约）：处理器运行**前**写 `job_started`（`job_id`、`name`、`params`），结果已知、确认消息**前**写 `job_result`（`job_id`、`name`、
   `succeeded`、`attempts`、`result`、`error`）。不给 `results` = 原行为（内存结果表）。
2. **重启语义**：已有 `job_result` 的任务**永不重跑**——重投的消息直接按存储的结果确认（解决"记录与确认之间崩溃会重跑一次"）。
3. **只开始、无结果的任务**（处理器内或写结果前进程死亡；已做了什么未知）——按本 ADR 第 3 条"重复只运行一次、幂等吸收"的规则选择：
   运行器无法知道处理器已产生的副作用，所以幂等性必须由调用方**声明**，不能推定。声明为幂等的处理器（`idempotent=`，无默认）在消息重投时重跑
   （再写一行 `job_started`）；其余一律停机待人工审查：`run_pending` 在轮询之前抛 `JobInterrupted`，消息不确认，`JobRunner.interrupted` 列出
   这些任务；解决方式是人工审查后换新的结果文件。与 ADR-0049 "只 started 未 recorded 的轮次 → 停机" 的原则一致（fail closed）。
4. **fail closed**：重开时先校验链（篡改 / 截断的行 → `JournalCorrupted`），再逐行做语义校验（未知行类型、字段不全或多余、`job_id` 不是名称 +
   参数的内容哈希、没有开始的结果、同一任务第二个结果、结果之后又开始、未声明幂等的任务被开始两次、不合法的结果字段 → `JobResultsCorrupted`，
   是 `JournalCorrupted` 的子类），从不跳过或修复。持久结果必须是 JSON（存储与返回的都是其 JSON 形式，重启前后相同）；持久写入失败 → 运行器
   停止、消息不确认；持久模式下键不是其内容身份的消息被拒（`ValueError`，否则其日志行重开时无法校验）。
5. **研究循环的轮次任务**：`ResearchLoop` 的轮次任务结果就是审计本身（ADR-0049），不另写结果日志。续接审计时，构造函数确认本循环已记录轮次
   的未确认轮次任务（内容身份逐一匹配；别的循环、未记录的轮次或其他内容的任务不动），因此持久总线在"记录与确认之间崩溃"后不会让下一次
   `run_unattended` 因"乱序"失败，也从不重跑。显式重复提交旧轮次仍按原样失败（"out of order"，阶段不运行）。
6. **接线**：上一条第 6 点的后续工作完成——`research/loop/compose.py` 在持久模式下不给 `bus` 时自动使用 `FileEventBus(state_dir/"bus")` 并与审计
   交叉核对（细节与取舍见 ADR-0049 同名实施说明）。调用方自带的总线照旧，不核对。
7. **测试**：`tests/apps/test_worker_jobs.py`（记录与确认之间崩溃后不重跑并按存储结果确认、内存模式仍重跑一次（原行为）、未声明幂等的中断任务
   停机且重启后仍停机、声明幂等的重跑一次并记录、篡改结果文件被拒、9 种重新成链的伪造历史被拒、非 JSON 结果与键不符的消息被拒、模块只依赖
   `apps` / `core` / 标准库）；`tests/apps/test_research_loop_durable.py`（重投的已记录轮次任务按审计确认、续跑与不中断相同；只确认本循环已记录轮次的
   任务；轮次发布失败停机；轮次消息是记录的纯函数）。
