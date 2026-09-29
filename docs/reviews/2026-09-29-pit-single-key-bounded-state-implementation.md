# PIT 单 key 中间状态有界化实现切片

**Phase：** Phase 1 / ADR-0077 §6.1.2  
**基线：** `codex/pit-edge-validation@9d27767`  
**状态：** 实现切片完成，待独立复核；不构成 E1-CAP-1 或 ADR-0077 infrastructure 验收。

## 已改动

- 单 key Canonical rows 继续由内容寻址 run 持有；selector 不再复制为 `revision_id -> row` 字典。owner event time 仅保留标量，选中 lineage / event time 通过可关闭 run reader 按需查找。
- Raw edge 按 edge ID 外部排序、跨日归并并逐项复核内容相等，再逐项映射到 Canonical precedence run。bounded 路径不再构造 `_mapped_edges` 的全量 `unique` / `mapped` 字典。
- 单 key 的日期遍历以首末日期标量逐日生成，不保留日期集合。
- 区间评估时刻经排序 run 去重后逐项读取，不构造全量 `changes` 集合 / 排序列表。
- effective availability 按 revision 从 key run 重读，不保留 `moved` 或 `available` 字典。
- 删除按所有已选 revision 增长的 `seen_revisions` 集合。固定知识 cutoff 下候选集合随 simulation time 单调增加；唯一 head 一旦被新候选取代就不会再次成为唯一 head，连续重复结果由既有 iterator 合并。
- legacy `select()`、`PointInTimeSelection.maximal_heads` 与持久化契约未改。

## 明确的剩余风险

本切片**仍不是**完整有界 PIT 实现，以下单 key O(N) 状态仍在：

1. selector 仍构造全量 `RevisionRecord` tuple 与 mapped `PrecedenceEvidence` tuple，以调用当前 `RevisionGraph` / `_heads` 实现。
2. `RevisionGraph` 仍建立 revision ID、arrival sequence、payload、ownership claims 与 DAG 校验结构；`maximal_heads` 仍建立 adjacency / reachability / superseded 集合。需要保留这些校验的同语义外存实现，不能以跳过校验替代。
3. conflict 时 `PointInTimeSelection.maximal_heads` 仍是完整 tuple，最大长度没有固定上限。输出形状决定另见 [冲突输出决策包](2026-09-29-pit-bounded-conflict-output-decision.md)，此切片没有替其作决定。
4. 每个 availability lookup 与选中 row lookup 会重读 key run，可能增加 I/O / CPU；这是以扫描换掉驻留映射，性能与完整 32 MiB 工作集尚未测量。
5. 每条 sorted-run object 的最大体积受参数限制，但 run 总对象数 / 存储空间随输入增长；PyIceberg / Iceberg metadata、单个 decoded run object、Arrow batch、run storage backend 与完整 E1 工作集仍需 ADR-0077 §10 测量和结构证明。

因此不能称单 key 历史已经完全有界，也不能宣称 E1-CAP-1 通过。下一步由独立复核者确认 parity / fail-closed 边界，再由 PM 决定继续替换图校验与 reachability 内部的实现批次；冲突 tuple 决策独立处理。

## 验证记录

- `tests/infrastructure/pit/test_selector_v3.py` 第一次整套运行：`1 failed, 19 passed`；唯一失败是新边界测试用 list 与旧 selector tuple 直接比较。该测试改为比较 tuple 后，精确复测：`1 passed`。
- 边 run 实现后，PIT v3 整套运行：`13 failed, 7 passed`；失败均为边界排序键将 `datetime` 直接传给 `canonical_json`。排序键改为唯一 edge ID 后，第二次整套运行：`2 failed, 18 passed`；两项均是旧测试仍 monkeypatch legacy `_mapped_edges`，bounded 路径现在使用 `_bounded_mapped_edge_run`。测试注入点随路径更新，并在 selector 增加错误 observation key 的 fail-closed 检查后，精确复测这两项：`2 passed`。
- Ruff check：`All checks passed!`；format check 与 `git diff --check` 通过。
- 全文件 mypy strict 仍有仓库既有错误；本切片相关剩余诊断包括迭代器 `.close()` 的 Protocol 类型缺口。未运行容量 probe。
