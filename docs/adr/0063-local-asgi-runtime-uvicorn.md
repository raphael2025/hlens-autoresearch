# ADR-0063: 本机只读研究 API 的 ASGI 运行时——Uvicorn（仅 127.0.0.1）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-27，Codex 依 Raphael 2026-09-23 授权决定技术栈）；实施（B65）以新增 Python 依赖为前提，见 §实施前提 |
| 日期 | 2026-09-27 |
| 决策者 | Codex（技术协调者，CLAUDE.md §0） |
| 起草者 | Claude Code（Opus），只记录决定，不改变决定 |
| 相关 Phase | 全栈（ADR-0048 只读 API 与控制台） |
| 影响范围 | Infrastructure（`apps/api` 的运行入口）；不改 Contract / Constitution / Data / Lifecycle |
| 是否破坏兼容 | 否 |

## 背景（Context）

ADR-0048 的只读研究 API（`apps/api`，`create_app(...)`）至今没有选定的 ASGI 服务器：测试用的是
`tests/apps/live_server.py`（仅测试用的最小 stdlib 服务器，不是生产服务器），`apps/api/README.md` 写明"生产部署用哪个 ASGI
服务器留待后续决定"，完成计划 §10.8 全栈行把"生产 ASGI 服务器未选定"列为未决。API 没有认证，只能在本机使用。

## 决策（Decision）

1. **运行时**：本机（WSL / 本地机器）只读研究 API 的 ASGI 运行时为 **Uvicorn**。
2. **范围仅限本机控制台**：严格绑定 `127.0.0.1`；单 worker；默认不开 reload。**绝不**把无认证的 API 暴露到 `0.0.0.0`、局域网或公网。
3. **框架无关**：`apps/api` 保持与服务器无关（`create_app(...)` 不 import Uvicorn）；Uvicorn 是可选的 `api-server` 依赖（extra），
   不进入默认依赖。
4. **入口**：提供一个有文档的可运行入口，把既有 `create_app` 设置（`knowledge`、`reports_root`、`jobs_results`、`jobs_idempotent`）
   接到 Uvicorn；入口本身拒绝非回环地址。
5. **不在范围内（须另行决定）**：公网部署、TLS / 认证、反向代理、高可用 / 多 worker。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. Uvicorn（选定） | FastAPI / Starlette 的标准 ASGI 服务器；单进程即可；支持优雅停止 | 新增一个 Python 依赖 | 选定 |
| B. 继续用测试服务器 `tests/apps/live_server.py` | 无新依赖 | 仅测试用、每连接一个请求，不是服务器实现 | 不能作为运行时 |
| C. Hypercorn / Granian 等 | 功能相当或更多 | 本机只读用途不需要其额外能力 | 无必要 |

## 后果（Consequences）

- 正面：控制台有一个明确、可文档化的本机后端运行方式；§10.8"生产 ASGI 服务器未选定"对本机只读用途关闭。
- 负面：多一个可选依赖；公网 / 认证 / TLS 仍未决定，本 ADR 不授权任何对外暴露。

## 实施前提（2026-09-27 记录）

- 离线解析已验证（只在草稿副本上，未改项目文件、未安装）：在 `[project.optional-dependencies] api-server = ["uvicorn>=0.30"]`
  下 `uv lock --offline` → `Resolved 53 packages … Added uvicorn v0.53.0`；唯一新增包为 `uvicorn`（`click`、`h11` 已在锁文件中），来源为本机 uv 缓存。
- 把它装入项目 `.venv`（`uv sync --extra api-server`）属于 CLAUDE.md §0 / H12 的"安装软件"，需 Raphael 明确授权后才实施 B65 代码与测试
  （真实 Uvicorn 子进程在回环地址上的健康检查、优雅停止与已配置的只读端点；无外网、无数据库）。

## 合规检查

- [x] 不修改 Domain Contract、Schema、Constitution、Validation Profile
- [x] 不引入对外暴露；无认证 API 只绑定 127.0.0.1
- [x] Domain 层仍无具体技术依赖（Uvicorn 只在 `apps/` 的可选入口）
