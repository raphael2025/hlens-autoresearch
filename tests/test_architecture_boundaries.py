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


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


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


def test_apps_do_not_import_research_plane() -> None:
    """apps/ 运行时不得 import research/（01-system.md §3 边界规则 1）。"""
    for path in _python_files("apps"):
        leaked = _imported_roots(path) & {"research"}
        assert not leaked, f"{path.relative_to(REPO)} 违反 Research/Application 平面边界"


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
