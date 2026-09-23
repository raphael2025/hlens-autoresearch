"""文档一致性测试。

把治理规则变成可执行检查：Constitution 不含数值、RETIRED 与 FAILED 的记录位置分离、
生命周期顺序、ADR 索引与 ADR 文件状态一致、链接可解析。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = sorted(p for p in REPO.rglob("*.md") if ".venv" not in p.parts and ".git" not in p.parts)
CONSTITUTION = REPO / "docs" / "research" / "constitution.md"
ADR_INDEX = REPO / "docs" / "adr" / "README.md"


def test_constitution_contains_no_numeric_thresholds() -> None:
    """ADR-0007：数值属于 Validation Profile，不属于 Constitution。"""
    lines = [
        line
        for line in CONSTITUTION.read_text(encoding="utf-8").splitlines()
        if not line.startswith("| 0.") and "数值占位" not in line
    ]
    body = "\n".join(lines)
    for pattern in [r"TBD-\d", r"[≥≤<>]\s*\d", r"\d+\s*(个月|笔|次交易)"]:
        match = re.search(pattern, body)
        if match is not None:
            context = body[max(0, match.start() - 40) : match.end() + 20]
            pytest.fail(f"Constitution 出现数值阈值：{context!r}")


def test_retired_never_enters_failure_registry() -> None:
    """ADR-0006 §3 第 2 条。"""
    for path in DOCS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"terminal_state`? \| ([^|\n]*RETIRED)", line):
                pytest.fail(f"{path.relative_to(REPO)}: terminal_state 仍包含 RETIRED")
            if re.search(r"RETIRED[^\n]*(进入|写入)\s*Failure Registry", line) and not re.search(
                r"(不进入|不写入|是否)", line
            ):
                pytest.fail(f"{path.relative_to(REPO)}: {line.strip()[:60]}")


def test_lifecycle_order_is_paper_before_production_candidate() -> None:
    """C-1 选项 B；旧顺序不得残留在正式文档中。"""
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert "OOS --> PRODUCTION_CANDIDATE" not in text, f"{path.relative_to(REPO)} 保留了旧顺序"
        assert not re.search(r"PRODUCTION_CANDIDATE\s*-->\s*PAPER", text), (
            f"{path.relative_to(REPO)} 保留了旧顺序"
        )


def test_reproducibility_tuple_documents_both_rule_versions() -> None:
    text = (REPO / "docs" / "architecture" / "06-experiment.md").read_text(encoding="utf-8")
    assert "constitution_version" in text
    assert "validation_profile_version" in text


def test_adr_index_matches_adr_status() -> None:
    index = ADR_INDEX.read_text(encoding="utf-8")
    for path in sorted((REPO / "docs" / "adr").glob("0*.md")):
        if path.name.startswith("0000"):
            continue
        head = path.read_text(encoding="utf-8").split("## 背景")[0]
        status = "Accepted" if "Accepted" in head else "Proposed"
        rows = [ln for ln in index.splitlines() if ln.startswith("|") and f"]({path.name})" in ln]
        assert rows, f"{path.name} 未登记到 ADR 索引"
        assert status in rows[0], f"{path.name} 状态与索引不一致：{rows[0].strip()}"


def test_markdown_links_resolve() -> None:
    for path in DOCS:
        for match in re.finditer(r"\]\(([^)#]+\.md)(#[^)]*)?\)", path.read_text(encoding="utf-8")):
            target = (path.parent / match.group(1)).resolve()
            assert target.exists(), f"{path.relative_to(REPO)} 链接失效：{match.group(1)}"


def test_code_fences_are_balanced() -> None:
    for path in DOCS:
        assert path.read_text(encoding="utf-8").count("```") % 2 == 0, (
            f"{path.relative_to(REPO)} 代码块未闭合"
        )
