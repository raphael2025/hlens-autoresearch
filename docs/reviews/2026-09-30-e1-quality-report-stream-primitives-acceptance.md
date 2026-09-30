# E1 Quality report stream primitives acceptance

**Result: ACCEPT for the generic bounded stream tree primitive only.** This does not accept concrete Quality report projections, reporter/Dataset integration, E1-CAP-1, or Phase 1.

## Scope

The development branch `codex/e1-adr0093-jsonl-stream` was based on `codex/e1-phase1-progress@dd3a965`. Its final commit was `0dfbdd164be484caf46e704c30d5389b3a03bd7d`; it was integrated as four ordered commits ending at `6293100` on the isolated integration candidate.

Changed files:

- `infrastructure/quality/report_streams.py`
- `tests/infrastructure/quality/test_report_streams.py`

The module writes and reads canonical JSONL content-addressed trees for the three ADR-0093 stream names. Every leaf/fanout bound is explicit. Empty streams use a childless level-one index root with zero records and leaves. A completed writer releases its index frontier. The reader validates reference types and structure, authenticates object keys/digests/sizes, and yields records in ordinal order through a closable reader. The primitive deliberately does not define domain record schemas, ordering keys, event IDs, or digest semantics; those belong to report rules.

## Three-way independent QA

All three roles accepted the final developer SHA `0dfbdd1`:

- Full direct suite: `33 passed in 0.20s`; Ruff: `All checks passed!`; formatting: `2 files already formatted`; mypy: `Success: no issues found in 2 source files`; `git diff --check` clean.
- Deep static review: ACCEPT. ADR-0093/0077 empty-root, ordinal, fanout, leaf bound, hashing, and ordered traversal rules remain intact. No concrete report projections were introduced.
- Boundary selection: `19 passed in 0.16s`, covering huge string allocation, infinite lazy Mapping, high-cardinality ordering/fanout, exact and over-limit records, frontier release, malformed descriptor fields, and wrong descriptor types.

The tree frontier is `O(fanout × depth)` with logarithmic depth in leaf count. Tests establish structural bounds but do not measure peak process RSS or exact CPython heap cost; the configured per-key accounting allowance is conservative rather than a heap-size proof.

## Integrated candidate verification

After sequentially cherry-picking the four development commits, the integrated candidate passed:

```text
uv run pytest -q tests/infrastructure/quality
62 passed in 33.86s

uv run pytest -q tests/infrastructure/catalog
189 passed, 59 skipped in 3.70s

uv run ruff check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
2 files already formatted

uv run mypy --follow-imports=silent infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
Success: no issues found in 2 source files
```

Catalog skips are not counted as passing; PostgreSQL append/replay remains unverified without the catalog URI.

## Remaining gates

- Define and independently accept each rule's exact event, revision, and gap projection/order and event digest encoding before connecting a reporter.
- Bound existing reporter/PIT/listing derivation state and define the ADR-0031 gap completeness proof for v3.
- Implement the new manifest commit/replay path, finite sorted snapshot-binding validation, and Dataset v3 read/write integration without changing the legacy report path.
- Measure complete process RSS on a main-matching clean SHA against the fixed 32 MiB E1-CAP-1 threshold.

E1-CAP-1 remains unproven and Phase 1 remains open.
