# apps/api

FastAPI 服务。职责：Registry / Experiment / Lifecycle 的 HTTP 入口，暴露 OpenAPI。只做编排与校验，不含业务规则。

> 框架已实现（ADR-0048，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`app.py` 的 `create_app`（`/health`、`/contracts`、`/lifecycle/transitions`、`/knowledge/search`），OpenAPI 由 `python -m apps.api.openapi` 导出到 `openapi.json`（测试校验其为最新）。
