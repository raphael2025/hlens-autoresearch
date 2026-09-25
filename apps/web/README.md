# apps/web

研究控制台：React + TypeScript + Vite + Apache ECharts。只通过 OpenAPI 生成的客户端（`src/api.d.ts`
+ `src/api.ts`）访问 `apps/api`；不含任何下单 / 转账 / 实盘账户 UI（H10），每个页面都带
`SIMULATED / NOT_VALIDATED` 横幅。

> 框架已实现（ADR-0048，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED；实现说明 2026-09-25 补充）：
> 5 个只读页面 —— Dashboard、Validation Reports、Research Loop、Lifecycle、Knowledge Search。
> 依赖已本地安装（`node_modules/`，已 gitignore），`npm run gen:api` 与 `npm run build` 均已跑通。

## 页面

| 页面 | 内容 | 数据来源 |
|---|---|---|
| Dashboard | 健康检查、契约数、各类报告计数 | `/health`、`/contracts`、`/reports/{kind}` |
| Validation Reports | 报告列表 + 详情（gate 结果表） | `/reports/validation_report[/​{id}]` |
| Research Loop | round 时间线、budget used 图表、失败数 | `/reports/research_loop_round` |
| Lifecycle | 允许的状态转移表 | `/lifecycle/transitions` |
| Knowledge Search | 知识条目检索（待检验主张，非结论） | `/knowledge/search` |

## 开发 / 构建

```bash
cd apps/web
npm install                 # 项目本地安装，node_modules/ 已 gitignore；用 systemd-run 包裹见下
npm run gen:api              # 从 ../api/openapi.json 生成 src/api.d.ts（提交该生成文件）
npm run dev                  # 本地开发服务器；/api/* 反代到 http://127.0.0.1:8000（vite.config.ts）
npm run build                # tsc --noEmit && vite build -> dist/
```

内存受限环境下（WSL，16GB 共享）用 `systemd-run` 包裹每条 npm 命令：

```bash
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm install
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm run gen:api
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm run build
```

后端需单独启动才能让 `npm run dev` 的代理生效（例如 `uvicorn` 跑 `apps.api.app:create_app`
的一个工厂实例，`reports_root` 指向一个写有 JSON 报告文件的目录）。这个目录由研究侧的
`research/reports`（[README](../../research/reports/README.md)）写入 —— `apps/api` 从不 import
`research/`，两边只通过 `<reports_root>/<kind>/<id>.json` 这份文件格式约定耦合。启动示例：

```python
# 先用研究侧的 writer 把报告写进某个目录，例如：
#   from research.reports import write_validation_report
#   write_validation_report(Path("var/reports"), report)
# 再让 apps/api 指向同一个目录：
import uvicorn
from pathlib import Path
from apps.api import create_app

app = create_app(reports_root=Path("var/reports"))
uvicorn.run(app, host="127.0.0.1", port=8000)
```

`reports_root=None`（工厂默认值）等价于没有配置报告目录：所有 `/reports/*` 端点返回空列表 / 404，
而不是报错。

## OpenAPI 类型

`src/api.d.ts` 是 `npm run gen:api` 的产物并已提交；改动 `apps/api` 的端点后必须先
`python -m apps.api.openapi`（重新导出 `apps/api/openapi.json`），再 `npm run gen:api` 重新生成,
两者都提交。`src/api.ts` 是在生成类型之上的一层薄 fetch 封装（唯一允许直接写 HTTP 调用的地方）。
