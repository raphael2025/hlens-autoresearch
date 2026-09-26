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

### K4 — Phase 1 PIT 边重复映射仍是独立验收阻断项

此前对跨日 R3 复核发现，公开 `PitSelector.select` 结果可含重复 `edge_id`：旧实现定向回归测试为 1 failed、7 passed；独立修复分支上对应测试与 selector 回归为 57 passed。修复尚未进入 Phase 1 候选分支，也没有候选分支上的门禁证据。

**决定：** 不接受 Phase 1，直到修复按拥有者公布的 push checkpoint 集成到候选分支，定向回归、相应 Phase 1 全量门禁和代码复核均通过。全代码分支不重复修改 PIT 文件。

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
