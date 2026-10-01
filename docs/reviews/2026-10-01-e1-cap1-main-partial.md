# E1-CAP-1 主线探针中断记录（2026-10-01）

## 结论

本次完整协议**已中断，不是 PASS 或 FAIL**。探针报告 `status=interrupted`、`capacity_verdict=ERROR`、`e1_cap1_evidence=false`。该结果没有改变 E1-CAP-1、DQ-9 或 Phase 1 状态。

## 运行与观测

- 代码基线：干净 detached `main@b5f80feb7c7d8295e83d3d4cebd11f28a2c351f1`，探针记录 `code_line.matches_main=true`。
- 配置：`M=256`；`N=10,000 / 100,000 / 500,000`；3 repeats；6 GiB cgroup；scratch 位于 ext4。
- 10,000 行 repeat 0 完成全部六个 stage。RSS 增量：`verify_archive 43.4 MiB`、`write_crash 54.8 MiB`、`resume 48.2 MiB`、`replay 45.0 MiB`、`read_batch 32.3 MiB`、`metadata 2.2 MiB`。
- 100,000 行 repeat 0 的 `verify_archive` 完成：`45.7 MiB`，用时 `366.084 s`，证明该阶段对归档重复执行完整严格解析，工作量随 391 个 microbatch 窗口快速增长。此单阶段值也超过 32 MiB，但协议未完成，因此整体容量结论仍记为中断 / 未判定，不得外推至其它阶段或 N。
- 完成该 100,000 行 stage 后收到 SIGTERM；未执行其余阶段、500,000 行或后续 repeats。JSON 的 ERROR 代表协议中断，不能当作容量 FAIL 结论。

## 后续候选实现

在 `phase/1` 候选工作区中，strict archive Arrow spool 现可在 pinned verifier 中最多缓存一个并跨窗口复用；解析输入与结果 spool 使用显式 canonical scratch 目录。该代码不属于上面的 main 探针，也不构成 E1-CAP-1 证据。候选需要独立 review；只有整合后的生产代码与 `main` 一致时，才可重跑正式矩阵。

候选修改后另跑 `N=1,000 / 2,000 / 3,000`、`M=256`、每个 N 单次的六阶段 smoke，探针状态为 `complete`、`code_line.matches_main=false`、`e1_cap1_evidence=false`。这仅确认修改后的 probe / scratch / spool 路径可执行；样本范围不足正式协议，不能以其 stage verdict 推导容量通过。

## 原始数据

- [中断 JSON](artifacts/e1-cap1-main-b5f80fe-partial-20261001.json)
- [RSS samples](artifacts/e1-cap1-main-b5f80fe-partial-20261001-samples.jsonl)
- [候选小规模诊断 JSON](artifacts/e1-cap1-phase1-archive-cache-small-20261001.json)
- [候选小规模 RSS samples](artifacts/e1-cap1-phase1-archive-cache-small-20261001-samples.jsonl)
