"""架构边界测试（roadmap Phase 0："契约层无基础设施依赖（导入检查测试）"）。

规则来源：docs/architecture/01-system.md §3–§4。
用 AST 静态扫描，不执行被检查的代码。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Domain / Contracts / Lifecycle 只允许依赖标准库与 Pydantic。
ALLOWED_THIRD_PARTY = {"pydantic"}

#: 明确禁止出现在契约层的基础设施与计算库（01-system.md §4）。
FORBIDDEN_IN_CORE = {
    "duckdb",
    "polars",
    "pandas",
    "numpy",
    "scipy",
    "sklearn",
    "statsmodels",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "pyiceberg",
    "boto3",
    "s3fs",
    "fastapi",
    "starlette",
    "uvicorn",
    "nats",
    "redis",
    "requests",
    "httpx",
    "anthropic",
    "openai",
    "litellm",
}

STDLIB_OK = {
    "__future__",
    "abc",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "hashlib",
    "itertools",
    "json",
    "math",
    "pathlib",
    "re",
    "typing",
    "uuid",
}


def _python_files(*relative: str) -> list[Path]:
    files: list[Path] = []
    for rel in relative:
        root = REPO / rel
        if root.exists():
            files.extend(p for p in root.rglob("*.py") if ".venv" not in p.parts)
    return files


def _imported_modules(path: Path) -> set[str]:
    """被导入模块的完整点分名（相对导入按本文件所在包解析）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.relative_to(REPO).parent.parts
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    modules.add(node.module)
            else:
                base = package[: len(package) - node.level + 1]
                modules.add(".".join((*base, node.module) if node.module else base))
    return modules


def _imported_roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imported_modules(path)}


def test_core_has_no_infrastructure_dependencies() -> None:
    for path in _python_files("core"):
        roots = _imported_roots(path)
        forbidden = roots & FORBIDDEN_IN_CORE
        assert not forbidden, f"{path.relative_to(REPO)} 引入了基础设施依赖：{sorted(forbidden)}"
        third_party = roots - STDLIB_OK - {"core"}
        assert third_party <= ALLOWED_THIRD_PARTY, (
            f"{path.relative_to(REPO)} 引入了未许可的第三方依赖：{sorted(third_party)}"
        )


def test_core_does_not_import_outer_layers() -> None:
    """Domain 不依赖任何外层（apps / research / plugins / strategies / risk）。"""
    outer = {"apps", "research", "plugins", "strategies", "risk", "infrastructure"}
    for path in _python_files("core"):
        leaked = _imported_roots(path) & outer
        assert not leaked, f"{path.relative_to(REPO)} 依赖了外层模块：{sorted(leaked)}"


def test_domain_does_not_import_other_core_packages() -> None:
    """Domain 不依赖任何层（01-system.md §4）——包括 core 内部的外层包。

    `core/lifecycle`、`core/contracts`、`core/compat` 都建立在 Domain 之上，
    因此 Domain 只能向内引用 `core.domain.*` 与 `core.errors`。共享词汇
    （例如 `ExecutionMode`）必须定义在 Domain，由外层重导出，而不是反向 import。
    """
    inner = {"core.domain", "core.errors"}
    for path in _python_files("core/domain"):
        leaked = sorted(
            module
            for module in _imported_modules(path)
            if module.split(".")[0] == "core"
            and not any(module == ok or module.startswith(f"{ok}.") for ok in inner)
        )
        assert not leaked, f"{path.relative_to(REPO)} 反向依赖了 core 的外层包：{leaked}"


def test_apps_do_not_import_research_plane() -> None:
    """apps/ 运行时不得 import research/（01-system.md §3 边界规则 1）。"""
    for path in _python_files("apps"):
        leaked = _imported_roots(path) & {"research"}
        assert not leaked, f"{path.relative_to(REPO)} 违反 Research/Application 平面边界"


#: 网络、交易所与凭据相关的库：执行服务在本构建中只有进程内模拟场所（ADR-0046 红线）。
FORBIDDEN_IN_EXECUTION = {
    "research",
    "socket",
    "ssl",
    "http",
    "urllib",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "websockets",
    "ccxt",
    "binance",
    "grpc",
    "infrastructure",
}


def test_execution_service_has_no_network_research_or_infrastructure_imports() -> None:
    """apps/execution 是独立的执行服务：不连研究平面、不联网、不直连基础设施（ADR-0046）。"""
    files = _python_files("apps/execution")
    assert files, "apps/execution 不存在"
    for path in files:
        leaked = _imported_roots(path) & FORBIDDEN_IN_EXECUTION
        assert not leaked, f"{path.relative_to(REPO)} 违反执行服务红线：{sorted(leaked)}"


def test_production_packages_do_not_import_research() -> None:
    """已晋升的生产代码不得 import 研究代码（H5 / ADR-0005）。"""
    for path in _python_files("strategies", "risk"):
        leaked = _imported_roots(path) & {"research"}
        assert not leaked, f"{path.relative_to(REPO)} 直接引用了研究代码"


@pytest.mark.parametrize(
    "package",
    ["core", "core/domain", "core/contracts", "core/lifecycle", "core/compat"],
)
def test_core_packages_are_importable(package: str) -> None:
    assert (REPO / package / "__init__.py").exists()
