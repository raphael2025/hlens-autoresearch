# ADR-0091: 只读登记处完整性审计

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | 本轮 PM（Raphael 指定 Codex 以项目经理身份执行底层代码缺口清单） |
| 起草者 | Codex（PM） |
| 相关 Phase | Phase 1 — Market Representation |
| 影响范围 | Infrastructure / Research registry audit；不改现有持久化格式 |
| 是否破坏兼容 | 否 |

## 背景（Context）

OPS-1 要为 Strategy、Profile Freeze、Retirement 与 Failure 四种登记处提供统一只读完整性核对。
已有登记处构造函数会创建目录 / 锁文件；前三种基于 hash-chained journal 的登记处还可能在打开时自动修复唯一的
record-written / anchor-not-written 崩溃窗口。Failure Registry 是无 hash chain 的 JSONL，且只用同一实例已见字节数
检测截断。直接调用现有打开 / 读取接口无法同时满足严格只读、禁止自动恢复和如实表达证据强度。

## 决策（Decision）

1. 新增登记处完整性审计入口必须是**只读快照**：不得创建目录或锁文件，不得获取会改变登记处状态的恢复路径，亦不得写
   journal、anchor 或 blob。若文件在核查期间变化，审计拒绝该快照，不重试写入或修复。
2. Strategy、Profile Freeze、Retirement 的审计按现有 journal 记录 / 业务规则校验，并只读校验提供的外部 anchor：
   - 有必需 anchor（Profile Freeze）时，anchor 与 journal 必须精确匹配；
   - 可选 anchor 未提供时，报告 `UNANCHORED`，明确无法检测整行尾部删除；
   - journal 比 anchor 多一条时报告未完成 / 不一致并 fail closed。常规构造函数会执行的单条 anchor 恢复不得由审计执行。
3. Failure Registry 保持现有 JSONL 格式。审计逐行解析并验证 `FailureRecord`，对坏行、部分尾行和读取期间变化 fail closed；结果标记为
   `STRUCTURAL_ONLY`，明确现有格式没有可信 hash chain / 外部 anchor，因而无法识别仍是合法 JSON 且仍满足 Schema 的历史改写或完整行删除。
   审计可以报告该次字节快照的 SHA-256，但不得将其表述为历史防篡改证据。
4. `infrastructure/tools/registry_audit.py` 输出四类登记处的独立结构化结果。CLI 仅接受显式路径，不创建登记处；一个登记处失败不
   隐藏其它登记处结果，整体状态反映失败项。PM 任务 OPS-1 的验收标准按本 ADR 更新：对现有格式能够证明的损坏应检出，各结果须标出无法证明的完整性边界。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 直接打开现有登记处并调用 `verify_integrity()` / `records()` | 复用现有业务校验代码 | 会创建目录 / 锁文件；哈希登记处可能修复 anchor；Failure Registry 读取会误报完整防篡改能力 | 违反只读与诚实证据要求 |
| 给 Failure Registry 引入 hash chain / 外部 anchor | 后续可提供更强的防篡改证据 | 改变既有存储格式与写路径；旧记录没有历史 anchor，迁移与信任根需另行设计 | 本任务不扩大既有持久化契约 |

## 后果（Consequences）

- 正面：审计不会悄悄改动或恢复被审查的状态；每种 Registry 的证据强度可被调用方区分。
- 代价：Failure Registry 的历史完整性无法由当前格式独立证明；无外部 anchor 的 Strategy / Retirement 同样无法检测尾部整行删除。
- 迁移：无；所有现有文件格式、写入行为与读取行为保持不变。
- 复现性：审计报告固定绑定读取快照的路径身份、记录数与可得的 hash；不改变实验或登记处内容。

## 合规检查

- [x] 不破坏已冻结契约，持久化格式不变
- [x] 不修改 Validation Constitution 或验证阈值
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- `docs/plans/2026-09-28-remaining-code-gaps.md` §2 OPS-1
- `infrastructure/registry/registry.py`、`profile_freeze.py`、`retirement.py`
- `research/strategies/failure_registry.py`
- `infrastructure/event_bus/journal.py`
