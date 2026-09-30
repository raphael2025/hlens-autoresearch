# E1 Quality report v3 evidence-gap projection acceptance

**Result: ACCEPT for the canonical-partition v3 evidence-gap stream projection only.** This does not accept end-to-end gap completeness, the v3 reporter, manifest commit/replay, Dataset integration, E1-CAP-1, or Phase 1.

## Scope

Development branch `codex/e1-quality-gap-projection` ended at `fdd97530e6b9b09de1cfe0b5047c93aeedfbf4a4`; its three commits were cherry-picked in order to the isolated integration candidate, ending at `94c743e`.

Changed files:

- `infrastructure/quality/report_projection.py`
- `tests/infrastructure/quality/test_report_projection.py`

The projection streams `(table, revision_id, gap)` inputs into a bounded RunSet and produces immutable fixed records with `quality_report_id`, `table`, `revision_id`, and `gap`. It sorts by the stable identity `(table, revision_id)` using Unicode code-point order without normalization, and rejects repeated identities even when the gap text differs. `subject_symbol` and `subject_start` remain bound by the manifest; legacy `batch_index` is excluded. It checks both canonical JSONL output size and the actual `{"$obj": record}` RunSet scratch row size before the builder can write. It allows exactly one stream per projector and rejects reentry, so per-call sorting cannot produce a globally out-of-order stream. Completion is only marked after full consumption and normal context exit; early close releases readers and does not advance the committed count.

The rule spec/hash now documents the gap record fields, identity, ordering, empty stream, and explicit byte bound. Legacy v1/v2 reporter and `_GapWriter` code were unchanged.

## Three independent QA roles

All three roles accepted final developer SHA `fdd97530e6b9b09de1cfe0b5047c93aeedfbf4a4`:

- Full Quality regression: `101 passed in 35.86s`; Ruff passed; format check reported `2 files already formatted`; mypy succeeded for 2 source files; diff-check clean.
- Boundary QA: `7 passed in 0.35s` on one-shot/reentry, immutable records, duplicate identities, high-cardinality sorting, early close, and empty streams. Earlier focused checks passed byte-limit and scratch-envelope exact/over boundaries and zero-write rejection.
- Static review: ACCEPT. It confirmed the one-shot guard closes the cross-call ordering and duplicate-detection gap; the result fields and completion state cannot be changed through normal attributes.

## Integrated candidate verification

On the integrated candidate:

```text
uv run pytest -q tests/infrastructure/quality/test_report_projection.py tests/infrastructure/quality/test_report_streams.py tests/infrastructure/quality/test_reporter.py tests/infrastructure/quality/test_listing_report.py
101 passed in 34.86s

uv run ruff check infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
2 files already formatted

uv run mypy --follow-imports=silent infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
Success: no issues found in 2 source files

git diff --check HEAD^ HEAD
```

The diff check exited 0 with no output.

## Remaining gates

- Connect event, revision, and gap projections to the canonical v3 reporter and prove that every expected gap is represented under the pinned inputs.
- Implement manifest commit/replay and finite snapshot-binding validation, then connect Dataset v3 reads and writes.
- Measure complete process RSS on a main-matching clean SHA against the 32 MiB E1-CAP-1 threshold.

E1-CAP-1 remains unproven and Phase 1 remains open.
