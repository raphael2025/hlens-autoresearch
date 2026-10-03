# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。重构主策略见 `REFACTOR_TARGET.md`，技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前焦点 | **Phase 1 敏捷垂直切片验收**（Binance BTC / ETH 1m K 线 + aggTrades），定义见 `REFACTOR_TARGET.md` §5、§6 |
| 重构状态 | **已确认**（2026-10-03，ADR-0111）；归档与切片尚未执行，计划见 [Action Plan](docs/plans/2026-10-03-archive-and-bypass-action-plan.md) |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`） |
| 代码基线 | `main@25ffada5`；`phase/1` 只多文档提交 |
| 契约 | 2.6.0（ADR-0109）；Schema 148 份，原地冻结 |
| 验证状态 | 全仓门禁最近一次通过于 `main@25ffada5`（2026-10-03）。门禁通过不等于任何 Phase 已验收。 |
| 最后更新时间 | 2026-10-03 |

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成 |
| 1 | Market Representation | 🔄 **唯一开启的 Phase**；关闭条件改为垂直切片验收（`REFACTOR_TARGET.md` §6）。原 Iceberg 数据平面原地冻结 |
| 0.5 | Public Knowledge Base | ❄️ FROZEN / ARCHIVED |
| 2 | Market State Engine | ❄️ FROZEN / ARCHIVED |
| 3 | Event & Interaction Engine | ❄️ FROZEN / ARCHIVED |
| 4 | Outcome Engine + 最小验证门 | ❄️ FROZEN / ARCHIVED |
| 5 | Strategy Library + 回测 | ❄️ FROZEN / ARCHIVED |
| 6 | State × Strategy | ❄️ FROZEN / ARCHIVED |
| 7 | Dynamic Discovery | ❄️ FROZEN / ARCHIVED |
| 8 | Validation & Robustness | ❄️ FROZEN / ARCHIVED |
| 9 | Synthetic Market Lab | ❄️ FROZEN / ARCHIVED |
| 10 | Dynamic Strategy Router（纸面） | ❄️ FROZEN / ARCHIVED |
| 11 | Continuous Research Loop | ❄️ FROZEN / ARCHIVED |
| 12 | Strategy Evolution | ❄️ FROZEN / ARCHIVED |
| 13 | Production Adaptive System（仅模拟） | ❄️ FROZEN / ARCHIVED |
| 14 | Technology Migration | ❄️ FROZEN / ARCHIVED |
| apps | api / worker / web | ❄️ FROZEN / ARCHIVED（worker 的数据集入口随 Phase 1 数据平面原地冻结） |

图例：❄️ FROZEN / ARCHIVED = 代码已写、从未验收、停止一切投入；代码目前仍在原位置，按 Action Plan 迁入 `archive/`（清单见 `REFACTOR_TARGET.md` §2）。切片验收前不重开。

## 3. 已完成

- ✅ Phase 0：契约、状态机、研究宪法 1.0.0，tag `phase-0-complete`
- ✅ 工程基线：Python 3.13 + uv、Git 与独立 GitHub 远程（公开发布决定见 ADR-0112）
- ✅ Binance 公共归档的下载（含 CHECKSUM）、严格解析与本地原子发布——切片继续复用
- ✅ 2026-10-03 三路只读审计（文档与授权 / 契约与数据管线 / 策略逻辑与代码结构）
- 🧱 Phase 0.5、2 ~ 14 与 Iceberg 数据平面的代码已写完，但从未验收，现已冻结（历史见 Git 与 `docs/reviews/`）

## 4. 当前正在做

- 公开发布准备（ADR-0112）：依据 Raphael 直接指令，补充许可证、开源说明和审查证据；推送/可见性以 GitHub 实际状态为准。

- ✅ ADR-0111 Accepted：`REFACTOR_TARGET.md`、`CLAUDE.md` 单一授权表、本文件重置、Action Plan
- ⏹ ADR-0108 §9(e) 预填充探针：Raphael 决定停止；检查时已无探针进程在运行，K 轴没有结果入库

## 5. 下一步

### 我（Raphael）需要做

- 看一遍 Action Plan，回复是否开始执行批次 A0

### Claude Code 需要做

- 按 Action Plan 执行 A0 ~ A5（归档）与 B0 ~ B4（切片），每批门禁全绿、独立 commit

## 6. 当前待决策

| ID | 事项 | 状态 |
|---|---|---|
| D-AUDIT-PIT | 新切片的数据版本与双轴时间保障范围 | 独立审查提出，待决定 |
| D-AUDIT-SPOT | 现货切片的现金与卖空边界 | 独立审查提出，待决定 |
| D-AUDIT-RUN | 完整复现输入与诊断运行的规则边界 | 独立审查提出，待决定 |
| D-AUDIT-ARCHIVE | 按依赖调整归档批次并保留参考校验 | 独立审查提出，待决定 |

详见 [独立架构审查](docs/reviews/2026-10-03-independent-architecture-audit.md) §8。仅为审查建议，未改变已批准的重构范围或执行正式架构变更。

**已决定（2026-10-03，ADR-0111）**：D-REFACTOR（重构目标）、D-AUTH-CONFLICT（单一授权表，已关闭）、D-PROBE（停止 K 轴探针）。

**随重构冻结（不再推进，记录保留）**：E1-CAP-1（K = 0 已证据级 PASS，K 轴未完成）、D-META-AGE、D-CATALOG-TABLES、D-09、P11-RUN、P0.5-REVIEW、STAGING-RETAIN。此前已决定事项的索引见 ADR 与 Git 历史。

## 7. 当前风险

- ⚠️ 真实数据极少：仓库内只有 BTC / ETH 两天的 1m K 线，aggTrades 从未落盘到研究可读层；切片的第一步是数据
- ⚠️ 归档会牵动架构边界测试、文档链接测试与 `pyproject.toml`；必须分批迁移，每批门禁全绿
- ⚠️ 官方资料不能证明历史行情的公开时刻：切片的 `available_time` 是绑定在 manifest 里的研究假设
- ⚠️ 平滑缩放不消除过拟合：尺度参数仍是自由参数，必须预登记并计入试验次数
- ⚠️ 切片绕开了原有的 manifest 校验与 PIT 选择器；等价的保障靠 `asof` 强制过滤与截断不变性测试，尚未实现
- ⚠️ 契约层只校验结构与声明：传递依赖闭包、`LlmCall` 调用登记完整性等仍依赖已冻结的运行时服务
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史样本外区间在认知上不完全干净
- ⚠️ CI 未配置；本地数据无异地副本；WSL 内存约 15 GiB

## 8. 当前禁止事项

- ❌ 不进行任何实盘操作：不下单、不连接实盘账户、不使用交易凭据
- ❌ 不为提高回测表现修改验证规则、成本、切分、指标（H3）；不削弱测试（H4）；不删除失败实验或运行记录（H6）
- ❌ 不修改、不重开 FROZEN / ARCHIVED 的 Phase；保留代码不得 import `archive/`
- ❌ 不把切片的回测数字当作策略有效的证据；不晋升任何策略
- ❌ 不修改宪法原则；不修改、移动或删除旧项目与旧数据
- ❌ 不 force push、不改写已发布历史、不修改全局 Git 配置

## 9. 最近一次变化

> 只保留最近 5 条；更早记录见 Git 历史与 `docs/reviews/`。

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-10-03 | ADR-0111 Accepted（Raphael 确认） | Phase 0.5、2 ~ 14 标记 FROZEN / ARCHIVED；焦点收敛到 Phase 1 垂直切片；数据层改为直读 Parquet |
| 2026-10-03 | `CLAUDE.md` 单一授权表 | D-AUTH-CONFLICT 关闭；`AGENTS.md`、roadmap 同步 |
| 2026-10-03 | E1-CAP-1 正式矩阵（`main@25ffada5`，K = 0）证据级 PASS | 随重构冻结，记录保留 |
| 2026-10-02 | 第三轮代码收口 + 全仓门禁 | ADR-0101 ~ 0106、0108 ~ 0110 实现并合入 `main` |
| 2026-10-02 | 契约 2.6.0（ADR-0109） | Schema 148 份 |

## 10. 下一阶段进入条件

**Phase 1 关闭条件**（`REFACTOR_TARGET.md` §6）：真实 BTC / ETH 1m K 线与 aggTrades 落盘且 manifest 一致；`mvp_slice.py` 端到端运行且重跑按位一致；防前视测试通过；性能标准有实测记录；保留代码门禁全绿。

原 roadmap 验收矩阵 #1 ~ #21 不再是关闭条件；已有证据（#1 ~ #12、#17）保留在 `docs/reviews/`。

**重开任何冻结 Phase 的条件**：Phase 1 切片验收完成，且有新的 ADR 明确重开范围。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 看 [Action Plan](docs/plans/2026-10-03-archive-and-bypass-action-plan.md)，回复是否开始执行。
2. 系统没有下单能力，没有策略被验证或晋升——这些保持不变。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 批次 A0：隔离骨架（`archive/` 排除出门禁、新增“不得 import archive”边界测试）。
2. 批次 A1 ~ A5：分批 `git mv` 归档，每批门禁全绿。
3. 批次 B0 ~ B4：依赖、数据落盘、读取入口、`mvp_slice.py`、切片验收。
