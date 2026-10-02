# ADR-0102: State 运行、读回与诊断入口

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权） |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」）；本文含 2026-10-01 PM 复核后的分层修订 |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 2（Market State Engine） |
| 影响范围 | `infrastructure/state/`、`research/states/`（新增入口）、文档；不改 `core/`、`apps/` |
| 是否破坏兼容 | 否 |

## 背景（Context）

ADR-0035 / 0089 已实现 State 契约、执行器、`state.states` 表、`StateTable`、`StateResultStore` 与显式建表命令。但没有任何生产调用方把一次 State 运行落盘，也没有读回、列表或从落盘结果生成诊断报告的入口；ADR-0089 §1 明确不接入 provisioning、启动或 worker。

## 决策（Decision）

1. 新增 `python -m infrastructure.state.run_cli`，子命令：
   - `compute`：输入为已持久化的 `FeatureResult` JSON 文件、`StateSpec`（显式 JSON）与 Provider 引用（从内置插件注册表解析）；调用 `run_state`；默认只打印摘要，`--store PATH` 写入 `StateResultStore`，`--apply-table` 另写 `state.states`（需显式 catalog 设置）。
   - `show`：按 `result_hash` 从 store 读回并核验内容哈希。
   - `list`：列出 store 中的 `result_hash`。
2. 新增 `python -m research.states.report_cli`：从 `StateResultStore` 读取结果，调用 `diagnose` 与 `write_state_diagnostics`。放在 research 侧以守依赖方向。
3. 本批**不**接入 Dataset → Feature → State 的全链路输入（依赖 E1-CAP-1）、Settings 默认工厂、worker job 与 API 表读取；这些需要另立 ADR。
4. 同步文档：`docs/architecture/03-data.md` 增加 `state.states` 节（注明尚未在生产 catalog 创建）、`infrastructure/state/__init__.py` 文档与导出、`research/states/README.md` 分工表、ADR-0035 实现注记。
5. 所有入口默认零副作用；错误输出不泄露凭据；训练型规格无 seed 仍拒绝（ADR-0035）。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 让 `StateStage` 在循环中可选写 store | 改动最小 | 落盘与循环耦合，无独立读回入口 | ADR-0089 §5 要求显式存储入口 |

## 后果（Consequences）

- 正面：Phase 2 验收项"状态分布与转移统计可报告"有了真实入口。
- 负面 / 代价：约 650 行代码与测试，未测。
- 对复现性的影响：无；`result_hash` 语义不变。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## Implementation note（2026-10-01；PM 复核后修订分层）

- **分层修订（PM 复核）**：决策 1 中的 `compute` 不放在 `infrastructure`：Provider 来自 `plugins`，而 `infrastructure` 不得依赖 `plugins`
  （01-system.md §4；ADR-0087 entry point 只加载 manifest）；以 `importlib` 字符串惰性加载只是绕过静态边界测试，实质仍是依赖。
  因此 `compute` 位于 research 侧：`python -m research.states.run_cli compute`（`research/states/run_cli.py`），Provider 以静态 import 从
  `plugins.states` 解析（名称 → 类的固定表，与内置 state manifest 的一致性由测试检查；引用为 `name@version`，版本须与 descriptor 的
  `plugin_key` 相符）。`python -m infrastructure.state.run_cli` 只保留只读的 `show` / `list`（及共享的 `read_contract` / `open_store` /
  `summary_lines`），不 import 任何 plugins / research 模块。
- `compute`：FeatureResult 本身不含 feature 引用，因此输入是 `--feature REQUEST.json RESULT.json` 的请求 / 结果对（可重复），经
  `state_inputs` 绑定；所有请求必须共享同一组评估时刻。契约 JSON 以其记录的 `schema_version` 在 JSON 模式下重建（精确小数以文本存储）。
- `--apply-table` 只写已存在的 `state.states`（不隐式建表；建表仍是 `create_state_tables --apply`）；`show` / `list` 对不存在的 store
  目录报错而不创建。未预期异常只输出异常类型名，不输出设置 / DSN。
- `research/states/report_cli.py`：`--store`、`--spec`（提供 state space）、`--min-run`（无默认值）、可选 `--out`（不给则只打印 Markdown，不写文件）。
- `infrastructure/state/__init__.py` 额外导出轻量的 `StateResultStore` / `StateStoreCorrupted`；`StateTable` 等不导出（会加载 pyiceberg）。
- 未做（按本 ADR §3）：全链路 Dataset → Feature → State 输入、Settings 默认工厂、worker job、API 表读取。
