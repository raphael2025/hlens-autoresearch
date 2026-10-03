# ADR-0111: 重构目标——Phase 1 垂直切片、Phase 0.5 / 2 ~ 14 归档、直读 Parquet、单一授权表

| 字段 | 值 |
|---|---|
| 状态 | Accepted（2026-10-03；Raphael 在 Claude Code 主会话逐项确认） |
| 日期 | 2026-10-03 |
| 决策者 | Raphael |
| 起草者 | Claude Code（主会话），依据三路只读审计 |
| 相关 Phase | Phase 1；冻结 Phase 0.5、2 ~ 14 |
| 影响范围 | Data / Infrastructure / 授权 / Roadmap |
| 是否破坏兼容 | 否（第一阶段不改契约与 Schema；数据层为新增路径） |

## 背景（Context）

2026-10-03 的三路只读审计（文档与授权、契约与数据管线、策略逻辑与代码结构）得到以下事实：

- Phase 2 ~ 14 约 7.2 万行代码加约 6 万行测试，全部未验收；Phase 1 自身的 Iceberg 数据平面约 6.4 万行，也未验收。
- 仓库内真实行情只有 BTC / ETH 两天的 1m K 线；aggTrades 从未进入 Canonical 层；没有入口能对真实落盘数据跑通研究链路。
- 全仓没有向量化计算依赖；特征、状态、事件 runner 是 O(T²) 的逐行 Pydantic 重校验。
- Iceberg 路径依赖 PyIceberg 私有 API 并锁死 0.12.0；D-META-AGE 仍开放并阻断历史回填。
- `CLAUDE.md` §0 有三段互相取代的授权，另有多处规则引用不同的授权方（D-AUTH-CONFLICT）。

## 决策（Decision）

1. **`REFACTOR_TARGET.md` 成为最高指导文档。** 其红线（R1 ~ R8）与范围的变更需 Raphael 批准。
2. **Phase 0.5、2 ~ 14 与 apps 标记为 FROZEN / ARCHIVED。** 代码按 `REFACTOR_TARGET.md` §2 以 `git mv` 迁入 `archive/`，不删除。重开任何冻结 Phase 需要 Phase 1 切片验收完成加新的 ADR。
3. **研究数据层改为 DuckDB / Polars 直读 Parquet**，以不可变文件集 + manifest（内容哈希）作为数据集身份。本条**取代 ADR-0021 备选方案 C 的否决**；ADR-0021 的其余内容（PostgreSQL 不存行情、`file://` 本地存储、无 NATS）继续有效。Phase 1 的 Iceberg 数据平面原地冻结，ADR-0075 / 0108 的后续工作、E1-CAP-1 的 K 轴与 D-META-AGE 停止推进，记录保留。
4. **Phase 1 关闭条件改为 `REFACTOR_TARGET.md` §6 的切片验收。** roadmap 验收矩阵 #1 ~ #21 不再是关闭条件，已有证据保留。
5. **研究计算代码遵守 `REFACTOR_TARGET.md` §4**（连续缩放、向量化、截断不变性测试）；控制面状态机与解析器不受该规约限制。
6. **授权收敛为 `CLAUDE.md` §0 的单一授权表**（Owner = Raphael；Lead = Claude Code 主会话；执行者 = Codex、Cursor、子代理），关闭 D-AUTH-CONFLICT。此前的历次授权段落（2026-09-23、2026-09-28、2026-09-30）被该表取代；ADR-0025 与 roadmap 中“由 Codex 复核后推送”的表述改为“由 Lead 审阅后推送”。
7. 新增项目依赖 polars、duckdb、numpy（uv 锁定）。

**授权来源**：Raphael 于 2026-10-03 在 Claude Code 主会话下达重构目标，并对三项确认问题分别回答：重构草案“同意推荐方案”；授权表“同意推荐方案”；K 轴探针“停止探针”（执行时已无探针进程在运行）。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A 保留 Iceberg，继续解决 D-META-AGE | 不改已有决定 | 回填仍受阻；继续维护私有 API 补丁；研究链路仍不可用 | 投入与 Phase 1 切片目标不相称 |
| B 写入保留 Iceberg，只给研究加直读旁路 | 改动面小 | 两套读语义；D-META-AGE 不解决；仍需先让 aggTrades 走通 Iceberg 全链路 | 切片的数据到位仍被旧管线阻塞 |
| C 立刻整体替换并删除 Iceberg 代码 | 最干净 | 约 63 个文件、111 个测试文件同时变动，风险集中 | 改为原地冻结，切片验收后再决定 |

## 后果（Consequences）

- 正面：项目焦点单一；研究链路可在真实数据上以秒级运行；授权关系无歧义。
- 负面 / 代价：切片不再经过原有的 revision 图、PIT 选择器与 v3 manifest 校验；等价保障由写入时的向量化断言、`asof` 强制过滤与截断不变性测试提供，这些尚待实现。研究计算由 `Decimal` 改为 `float64`。
- 需要迁移的内容：`archive/` 迁移牵动 `tests/test_architecture_boundaries.py`、`tests/test_docs_consistency.py`、`pyproject.toml` 与两个 `__init__.py`；按 `docs/plans/2026-10-03-archive-and-bypass-action-plan.md` 分批执行。
- 对复现性的影响：既有记录与 Iceberg 数据保持可读；新切片的复现元组为（git commit、dataset_id、spec 哈希、依赖版本、availability 政策 ID）。

## 合规检查

- [x] 不破坏已冻结契约（第一阶段 `core/` 与 `schemas/` 不动）
- [x] 不修改 Validation Constitution；本决定与任何回测结果无关（H3）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变
- [x] 不删除失败实验、生命周期历史或已有审查记录（H6）

## 参考

- [REFACTOR_TARGET.md](../../REFACTOR_TARGET.md)
- [ADR-0021](0021-phase1-local-data-infrastructure.md)、[ADR-0025](0025-private-github-remote-and-reviewed-progress-push.md)、[ADR-0108](0108-canonical-commit-granularity-and-metadata-growth.md)
- [Action Plan](../plans/2026-10-03-archive-and-bypass-action-plan.md)
