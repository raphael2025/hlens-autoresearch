"""Phase 0.5: the reviewed knowledge write path and its CLI (ADR-0034 §4; knowledge-base.md).

Every test writes into ``tmp_path`` only, never into docs/research/knowledge.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.knowledge import KnowledgeProviderError, KnowledgeQuery
from core.domain.research import KnowledgeItem
from plugins.knowledge import (
    KnowledgeWriteError,
    LocalKnowledgeProvider,
    LocalKnowledgeStore,
)
from plugins.knowledge.cli import main
from plugins.knowledge.local import DEFAULT_ITEMS_DIR

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)

ITEM: dict[str, object] = {
    "kind": "knowledge",
    "name": "state_funding_basis",
    "version": "1.0.0",
    "created_at": "2026-09-26T00:00:00Z",
    "source": "Someone (2021). A paper on funding.",
    "license": "citation and own summary only",
    "claim": "Extreme funding precedes basis mean reversion.",
    "conditions": ["perpetual futures"],
    "evidence_level": "E2",
}


def _store(tmp_path: Path) -> LocalKnowledgeStore:
    return LocalKnowledgeStore(tmp_path, clock=lambda: NOW)


def _files(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir())


def test_an_added_item_is_searchable_and_carries_its_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.add(ITEM, reviewed_by="raphael")
    assert result.created and result.path == tmp_path / "item-state_funding_basis-1.0.0.json"
    assert _files(tmp_path) == [
        "item-state_funding_basis-1.0.0.json",
        "item-state_funding_basis-1.0.0.review",
    ]
    provider = LocalKnowledgeProvider(tmp_path)
    assert provider.items == (result.item,)
    hits = provider.search(KnowledgeQuery(terms=("funding",), name_prefix="state_"))
    assert [item.name for item in hits.items] == ["state_funding_basis"]
    review = store.review_of("state_funding_basis", "1.0.0")
    assert review.reviewed_by == "raphael"
    assert review.item == "state_funding_basis@1.0.0"
    assert review.item_hash == result.item.content_hash()
    assert review.reviewed_at == "2026-09-26T12:00:00+00:00"


def test_a_knowledge_item_instance_is_accepted(tmp_path: Path) -> None:
    item = KnowledgeItem.model_validate_json(json.dumps(ITEM))
    assert _store(tmp_path).add(item, reviewed_by="r").item == item


def test_an_identical_re_add_is_idempotent_and_keeps_the_first_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="first")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    again = store.add(ITEM, reviewed_by="second")
    assert not again.created
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert store.review_of("state_funding_basis", "1.0.0").reviewed_by == "first"


def test_a_new_version_is_a_new_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="r")
    store.add(dict(ITEM, version="1.1.0", claim="Revised claim."), reviewed_by="r")
    assert [item.version for item in LocalKnowledgeProvider(tmp_path).items] == ["1.0.0", "1.1.0"]


def test_same_name_and_version_with_different_content_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="r")
    before = _files(tmp_path)
    with pytest.raises(KnowledgeWriteError, match="different content"):
        store.add(dict(ITEM, claim="A different claim."), reviewed_by="r")
    assert _files(tmp_path) == before


def test_a_conflict_with_a_seed_file_is_refused_and_an_identical_seed_is_a_no_op(
    tmp_path: Path,
) -> None:
    (tmp_path / "seed.json").write_text(json.dumps([ITEM]), encoding="utf-8")
    store = _store(tmp_path)
    same = store.add(ITEM, reviewed_by="r")
    assert not same.created and same.path is None
    assert _files(tmp_path) == ["seed.json"]
    with pytest.raises(KnowledgeWriteError, match="different content"):
        store.add(dict(ITEM, evidence_level="E3"), reviewed_by="r")


@pytest.mark.parametrize(
    ("item", "match"),
    [
        (dict(ITEM, license=" "), "no source or licence"),
        (dict(ITEM, license=""), "no source or licence"),
        (dict(ITEM, source=" "), "invalid knowledge item"),
        ({k: v for k, v in ITEM.items() if k != "source"}, "invalid knowledge item"),
        (dict(ITEM, evidence_level="E9"), "invalid knowledge item"),
        (dict(ITEM, extracted_by="llm"), "invalid knowledge item"),
        (dict(ITEM, name="Bad Name"), "invalid knowledge item"),
    ],
)
def test_invalid_or_unsourced_items_are_refused(
    tmp_path: Path, item: dict[str, object], match: str
) -> None:
    with pytest.raises(KnowledgeWriteError, match=match):
        _store(tmp_path).add(item, reviewed_by="r")
    assert _files(tmp_path) == []


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_every_add_needs_a_named_reviewer(tmp_path: Path, reviewer: str) -> None:
    with pytest.raises(KnowledgeWriteError, match="reviewer"):
        _store(tmp_path).add(ITEM, reviewed_by=reviewer)
    assert _files(tmp_path) == []


def test_a_corrupt_items_directory_refuses_writes(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KnowledgeProviderError, match="unreadable item file"):
        _store(tmp_path).add(ITEM, reviewed_by="r")


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeWriteError, match="not a directory"):
        _store(tmp_path / "nope").add(ITEM, reviewed_by="r")


def test_an_edited_item_file_fails_its_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.add(ITEM, reviewed_by="r")
    assert result.path is not None
    result.path.write_text(json.dumps([dict(ITEM, claim="Edited.")]), encoding="utf-8")
    with pytest.raises(KnowledgeWriteError, match="changed after its review"):
        store.review_of("state_funding_basis", "1.0.0")


@pytest.mark.parametrize(
    "edit",
    [
        lambda raw: dict(raw, reviewed_by=""),
        lambda raw: dict(raw, format="other"),
        lambda raw: dict(raw, extra="x"),
        lambda raw: dict(raw, item_hash="0" * 64),
    ],
)
def test_a_tampered_review_record_is_refused(tmp_path: Path, edit: object) -> None:
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="r")
    _, review_path = store.paths_for("state_funding_basis", "1.0.0")
    raw = json.loads(review_path.read_text(encoding="utf-8"))
    review_path.write_text(json.dumps(edit(raw)), encoding="utf-8")  # type: ignore[operator]
    with pytest.raises(KnowledgeWriteError):
        store.review_of("state_funding_basis", "1.0.0")


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item_path, _ = store.paths_for("state_funding_basis", "1.0.0")
    item_path.write_text("[]", encoding="utf-8")  # a hand-written file that holds no item
    with pytest.raises(KnowledgeWriteError, match="not overwritten"):
        store.add(ITEM, reviewed_by="r")
    assert item_path.read_text(encoding="utf-8") == "[]"


def test_a_crash_after_the_review_record_is_recovered_by_the_same_add(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="r")
    item_path, review_path = store.paths_for("state_funding_basis", "1.0.0")
    os.unlink(item_path)  # simulate a crash between the two writes
    assert LocalKnowledgeProvider(tmp_path).items == ()
    with pytest.raises(KnowledgeWriteError, match="reviews a different item"):
        store.add(dict(ITEM, claim="Something else."), reviewed_by="r")
    assert store.add(ITEM, reviewed_by="later").created
    assert store.review_of("state_funding_basis", "1.0.0").reviewed_by == "r"


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    _store(tmp_path).add(ITEM, reviewed_by="r")
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _batch(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "batch" / "in.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_add_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    batch = _batch(tmp_path, [ITEM, dict(ITEM, version="2.0.0")])
    args = ["--reviewed-by", "raphael", "--items-dir", str(items_dir), str(batch)]
    assert main(["add", *args]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "added state_funding_basis@1.0.0",
        "added state_funding_basis@2.0.0",
    ]
    assert main(["add", *args]) == 0
    assert "unchanged state_funding_basis@1.0.0" in capsys.readouterr().out
    assert main(["verify", "--items-dir", str(items_dir)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "2 items load cleanly"


def test_cli_an_invalid_batch_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    batch = _batch(tmp_path, [ITEM, dict(ITEM, name="other", license="")])
    code = main(["add", "--reviewed-by", "r", "--items-dir", str(items_dir), str(batch)])
    assert code == 1 and "no source or licence" in capsys.readouterr().err
    assert list(items_dir.iterdir()) == []
    clash = _batch(tmp_path, [ITEM, dict(ITEM, claim="Other.")])
    assert main(["add", "--reviewed-by", "r", "--items-dir", str(items_dir), str(clash)]) == 1
    assert "appears twice" in capsys.readouterr().err
    assert list(items_dir.iterdir()) == []


def test_cli_verify_flags_an_unreviewed_store_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "item-state_funding_basis-1.0.0.json").write_text(
        json.dumps([ITEM]), encoding="utf-8"
    )
    assert main(["verify", "--items-dir", str(tmp_path)]) == 1
    assert "review record" in capsys.readouterr().err


def test_cli_verify_passes_on_the_repository_seed_base(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", "--items-dir", str(DEFAULT_ITEMS_DIR)]) == 0
    assert capsys.readouterr().out.strip().endswith("items load cleanly")


def test_cli_verify_fails_on_an_orphan_review_and_the_same_add_recovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex review K1: a crash between the review record and the item file must not verify."""
    store = _store(tmp_path)
    store.add(ITEM, reviewed_by="raphael")
    item_path, review_path = store.paths_for(str(ITEM["name"]), str(ITEM["version"]))
    item_path.unlink()  # the state a crash after the review record leaves behind
    assert review_path.exists() and not item_path.exists()
    capsys.readouterr()

    assert main(["verify", "--items-dir", str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert "incomplete add (review without item)" in err
    assert review_path.name in err and "re-run the same add" in err
    assert "load cleanly" not in out

    batch = tmp_path.parent / f"{tmp_path.name}-recover.json"
    batch.write_text(json.dumps(ITEM), encoding="utf-8")
    add = ["add", "--reviewed-by", "raphael", "--items-dir", str(tmp_path), str(batch)]
    assert main(add) == 0
    assert item_path.exists()
    assert main(["verify", "--items-dir", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert err == "" and "1 items load cleanly" in out
