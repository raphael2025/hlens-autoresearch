"""Durable ``LineageGraph``: parent/child relations survive a process restart (debugging pass,
2026-09-25; ADR-0045 implementation note; backlog row R26).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import StrategySpec
from research.evolution import LineageError, LineageGraph
from research.persistence import AppendOnlyJournal, JournalCorrupted

SIGNAL = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")


def _spec(name: str = "tsmom", **fields: object) -> StrategySpec:
    base: dict[str, object] = {
        "name": name,
        "version": "1.0.0",
        "signals": (SIGNAL,),
        "params": {"lookback": 20},
        "param_search_space": {"lookback": (10, 20, 40)},
    }
    base.update(fields)
    return StrategySpec(**base)  # type: ignore[arg-type]


def test_a_parent_child_relation_persists_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    parent = _spec("parent")
    child = _spec("child", lineage=(parent.ref,))

    process_a = LineageGraph([parent, child], path=path)
    assert process_a.parents(child.ref) == (parent.ref,)

    process_b = LineageGraph([], path=path)  # a fresh process opening the same ledger file
    assert process_b.parents(child.ref) == (parent.ref,)
    assert process_b.ancestors(child.ref) == (parent.ref,)
    assert child.ref in process_b.descendants(parent.ref)
    assert process_b.missing() == ()


def test_add_after_reload_is_durable_too(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    parent = _spec("parent")
    LineageGraph([parent], path=path)

    process_b = LineageGraph([], path=path)
    child = _spec("child", lineage=(parent.ref,))
    process_b.add(child)

    process_c = LineageGraph([], path=path)
    assert process_c.parents(child.ref) == (parent.ref,)


def test_re_adding_the_same_spec_after_reload_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    parent = _spec("parent")
    LineageGraph([parent], path=path)

    reloaded = LineageGraph([parent], path=path)  # same content, same ref
    assert reloaded.parents(parent.ref) == ()


def test_a_conflicting_respec_of_the_same_ref_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    parent = _spec("parent")
    LineageGraph([parent], path=path)

    other = parent.model_copy(update={"params": {"lookback": 999}})
    with pytest.raises(LineageError):
        LineageGraph([other], path=path)


def test_omitting_path_keeps_the_graph_purely_in_memory(tmp_path: Path) -> None:
    parent = _spec("parent")
    LineageGraph([parent])  # no path: nothing written to disk
    fresh = LineageGraph([], path=tmp_path / "unrelated.jsonl")
    assert fresh.parents(parent.ref) == ()


def test_a_tampered_lineage_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    LineageGraph([_spec("parent")], path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["name"] = "renamed"
    path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        LineageGraph([], path=path)


def test_an_unknown_record_type_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    AppendOnlyJournal(path).append("not_a_spec", {"x": 1})
    with pytest.raises(JournalCorrupted):
        LineageGraph([], path=path)


def test_deterministic_replay_gives_the_same_state(tmp_path: Path) -> None:
    path = tmp_path / "lineage.jsonl"
    parent = _spec("parent")
    child = _spec("child", lineage=(parent.ref,))
    LineageGraph([parent, child], path=path)

    a = LineageGraph([], path=path)
    b = LineageGraph([], path=path)
    assert a.parents(child.ref) == b.parents(child.ref) == (parent.ref,)
    assert a.missing() == b.missing() == ()
