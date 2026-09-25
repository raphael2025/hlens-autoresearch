# ADR-0048: API 服务与研究控制台骨架（apps/api、apps/web）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 影响范围 | `apps/api/`（新增项目依赖 `fastapi`，ADR-0002 既定栈）、`apps/web/`（npm 依赖声明，未安装） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `apps/api`：FastAPI 应用工厂 `create_app`（Provider 注入），只做编排与校验，不含业务规则、不 import `research/`；OpenAPI 导出到
   `apps/api/openapi.json` 并由测试校验为最新。首批只读端点：健康、契约清单、生命周期转移、知识检索。
2. `apps/web`：Vite + React + TypeScript 骨架，只经 OpenAPI 生成的类型访问后端（`npm run gen:api`）；依赖只声明、未安装（项目本地
   安装与构建属调试阶段；不涉及系统软件）。
3. 写操作（实验登记、生命周期推进）等 P7 / P8 / P11 框架后再暴露，并须经授权服务（ADR-0011 的主体与授权约束）。
