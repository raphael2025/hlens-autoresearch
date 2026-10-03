# Action Plan — `archive/` 剪枝与代码旁路（ADR-0111）

> 依据 [REFACTOR_TARGET.md](../../REFACTOR_TARGET.md) 与 [ADR-0111](../adr/0111-refactor-target-vertical-slice.md)。
> 规则：每批一个独立 commit；每批结束时保留代码的 pytest / ruff / ruff format / mypy 全绿；只用 `git mv`，不删除；不削弱测试（H4）——随模块归档的测试整体迁走，保留模块的测试一条不改断言。

## 批次

| 批 | 内容 | 完成标准 |
|---|---|---|
| A0 | **隔离骨架**：建 `archive/README.md`（说明来源、不可 import、如何重开）；`pyproject.toml` 把 `archive/` 排除出 pytest `testpaths`、ruff、mypy；`tests/test_docs_consistency.py` 的排除目录加入 `archive`；新增边界测试“保留代码不得 import `archive`” | 门禁全绿；新边界测试通过 |
| A1 | **research 归档**：`research/` 除 `sandbox/`（新建空包）外全部迁入 `archive/research/`；对应 `tests/research/`、8 个 `tests/infrastructure/e2e/test_research_*`、`tests/infrastructure/bars/test_dataset_bars*.py` 迁入 `archive/tests/` | `tests/test_architecture_boundaries.py:151` 的存在性断言按新布局改写（断言对象改为保留目录，强度不降） |
| A2 | **apps 归档**：api、execution、promotion、web 与 worker 的 loop / degradation / metrics 迁走；改 `apps/worker/__init__.py`；`test_architecture_boundaries.py:191,228` 同步 | `apps/` 只剩 worker 的 dataset_job、jobs、journal |
| A3 | **plugins 归档**：states、events、outcomes、knowledge、llm、synthetic、features 中除 `bars.py` 外的模块、`backtest/reference.py`；改 `plugins/features/__init__.py`；清理 `pyproject.toml` entry-points 中指向已归档模块的条目 | 插件发现测试只列出保留的插件 |
| A4 | **infrastructure 归档**：state、event、registry、event_bus、migration、content、plugins、`tools/registry_audit.py`；顶层后续 Phase 契约套件测试（`test_event_contract_suite.py` 等 8 个）迁走 | Phase 1 目录下的测试全部原样通过 |
| A5 | **文档同步**：`docs/architecture/00-overview.md`、`03-data.md` 标注冻结范围；`docs/plans/` 旧计划标注“已被 ADR-0111 取代”；`PROJECT_MEMORY.md` Compaction | 文档一致性测试通过 |
| B0 | **依赖**：`uv add polars duckdb numpy`，锁定版本 | `uv.lock` 更新；门禁全绿 |
| B1 | **数据落盘**：`research/sandbox/ingest.py`——复用归档下载与严格解析，写 §3 布局的 Parquet + manifest；写入时做向量化断言（时间单调、主键唯一、覆盖区间、`available_time ≥ 事件时间`）；先 BTC / ETH 各 30 天 1m + aggTrades，再扩到 1 年 | manifest 与 CHECKSUM 一致；断言失败整文件拒绝（有测试） |
| B2 | **读取入口**：`research/sandbox/lake.py::load`，`asof` 必填并强制过滤；manifest 校验 | “`asof` 之后的数据读不到”测试；加载耗时实测写回 `REFACTOR_TARGET.md` §5 |
| B3 | **切片**：`research/sandbox/mvp_slice.py`——特征、连续信号、向量化回测、指标、`data/runs/<run_id>/` 输出 | 同输入重跑按位一致；S6 截断不变性测试；S7 AST 检查测试 |
| B4 | **验收**：按 `REFACTOR_TARGET.md` §6 五条出验收记录（`docs/reviews/`）；合并 `main`；tag `phase-1-complete` | 验收记录 + 门禁输出 |

## 顺序与依赖

- A0 → A1 → A2 → A3 → A4 → A5 串行（每批都会动 `pyproject.toml` 或边界测试，不并行）。
- B0 可在 A0 之后任何时候做；B1 ~ B3 依赖 B0，与 A1 ~ A5 文件不重叠，可交给执行者并行。
- B1 需要网络下载官方公共归档（只写本机 `data/lake/`，不入仓库）。

## 风险与处理

| 风险 | 处理 |
|---|---|
| 动态 import 与 entry-points 未被 grep 覆盖 | 每批跑全仓保留测试；失败则回到上一个 commit 重新划界，不改断言 |
| 保留的 Phase 1 测试间接依赖被归档模块 | 该测试随被依赖模块一起评估：属于 Phase 1 语义则保留并把依赖留下，否则整体迁走；逐个记录在 commit message |
| `float64` 与原 `Decimal` 结果不一致 | 切片是新路径，不与旧结果比对；不改旧代码的数值语义 |
| 1 年 aggTrades 体积与内存 | 按 symbol-day 分文件流式写入；加载标准只针对单 symbol-day |
