# research/evolution

Phase 12 Strategy Evolution（[ADR-0045](../../docs/adr/0045-strategy-evolution.md)）。研究代码（H5）；无契约变化。
状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

| 模块 | 内容 |
|---|---|
| `operators.py` | `mutate`（只在声明的搜索空间内移动参数）、`combine`（参数冲突即拒绝）、`require_new_version`（拒绝就地修改 ACTIVE）、`retire`（追加式 `RetirementRecord`） |
| `lineage.py` | `LineageGraph`：祖先 / 后代 / 缺失祖先；可选持久（哈希链只追加日志） |
| `proposals.py` | 替换提案：`propose_replacement`、`ReplacementProposal`、`ProposalLedger` |

后代是新版本、从 `IDEA` 重新进入生命周期并重新验证；父代不变。循环中的调度见 `research/loop/evolution.py`。

## 替换提案（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

演化只能**提出**"用已重新验证的后代替换运行中的策略"，从不批准、晋升或替换。`propose_replacement` 先核对，再记录：

- 现任策略的生命周期历史属于它，且处于 `ACTIVE` 或 `DEGRADED`；
- 候选是新版本且源自现任（`require_new_version`），谱系图能从候选追溯到现任，候选的所有祖先都已记录、候选内容与谱系中记录的一致；
- 候选在**自己的**历史上重新通过验证：处于 `PAPER` 或 `PRODUCTION_CANDIDATE`（不从父代继承状态）；
- 至少一项非空证据引用（如验证报告哈希）、非空理由与提出者；时间戳必须带时区。

结果 `ReplacementProposal` 的 `status` 恒为 `PENDING_HUMAN_APPROVAL`（构造参数里没有 `status`，载荷里写其他值即拒绝），
`proposal_hash` 覆盖全部载荷；执行提案是人在正常 Promotion 路径上的生命周期转移（ADR-0005 / ADR-0006），不在研究平面。
`ProposalLedger(path)`：哈希链只追加日志，重开时逐条重建并复核哈希；相同提案重复记录不追加；没有批准 / 编辑 / 删除方法，
未知记录类型或写入"已批准"的行一律 `JournalCorrupted`。

已知限制：`require_new_version` 拒绝与现任**版本号相同**的候选（即使名称不同），因此 `combine` 产出的 `1.0.0` 不能替换 `1.0.0` 的现任——保守，保持不变；
提案尚未接入持续循环（跨阶段接线，后续串行处理）。测试：`tests/research/evolution/test_replacement_proposals.py`。
