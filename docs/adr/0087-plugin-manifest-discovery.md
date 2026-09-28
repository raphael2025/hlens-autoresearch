# ADR-0087: 插件 Manifest 与发现加载的实现

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权（CLAUDE.md §0） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | 跨 Phase（Plugin 架构） |
| 影响范围 | Infrastructure（新增插件加载模块）、`pyproject.toml` entry points；**不改** `core/` 契约与既有 Provider 行为 |
| 是否破坏兼容 | 否：现有显式构造 / 登记路径保持可用 |

## 背景

`docs/architecture/05-plugin.md` §4–5 规定了插件清单（Manifest）以及基于 Python entry points（`hlens.plugins.<kind>`）的发现与 Registry 校验。2026-09-28 的 MOD-FEAT 审计发现，没有任何 Provider 类型实现了这套机制：现有 Provider 都由代码直接 import 和构造。

## 决策

1. 新增 `infrastructure/plugins/`：
   - `manifest.py`：按 05-plugin §4 的字段定义 Manifest 数据类与严格解析。缺字段、多余字段、非法 SemVer、未知 kind 一律拒绝。
   - `discovery.py`：用 `importlib.metadata.entry_points(group="hlens.plugins.<kind>")` 发现插件，逐个校验三项：
     - `contract_version` 与当前契约 major 兼容；
     - Manifest 合法；
     - `params_schema` 是合法的 JSON Schema 子集。
     任何一个插件校验失败，**整体 fail closed** 并报告失败的插件，不静默跳过。
   - 发现结果交给调用方显式登记，不自动修改任何全局 Registry。
2. 为现有内置 Provider 编写静态 Manifest，并在 `pyproject.toml` 声明对应的 entry points。
   - 声明后要重新安装项目（`uv sync`）才会生效，这一步留到调试阶段执行。
   - 在此之前，发现模块的测试用可注入的 entry point 源，不依赖安装状态。
3. 隔离（05-plugin §6）沿用现有进程内模型；本 ADR 不引入子进程沙箱。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 维持纯显式 import | 与已写入架构文档的设计不一致，第三方插件也无法接入 |
| 自研目录扫描发现 | 05-plugin §5 已经选定 entry points |

## 后果

- 正面：插件机制与架构文档一致，第三方 Provider 有了标准接入路径。
- 代价：多了一处 Manifest 与实现可能不一致的风险。缓解办法是写测试，逐一比对内置 Manifest 与 Provider 的 name / version / kind。
