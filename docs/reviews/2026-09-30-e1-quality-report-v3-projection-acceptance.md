# E1 Quality report v3 event projection acceptance

**Result: ACCEPT for the canonical-partition v3 event/revision projection slice only.** This does not accept the v3 reporter, manifest commit/replay, Dataset integration, E1-CAP-1, or Phase 1.

## Scope

Development branch `codex/e1-quality-report-projection` ended at `ed3399dfbbf9f6818ef256fd0d9913bfdc248075`; its four commits were cherry-picked in order to the isolated integration candidate, ending at `41a4ede71cc4fa9bfc566aa4490bc8ca7755b2db`.

Changed files:

- `infrastructure/quality/report_projection.py`
- `tests/infrastructure/quality/test_report_projection.py`

The new projection stages revision IDs through a bounded RunSet, sorts and deduplicates them, computes a versioned SHA-256 event ID over the rule identity, all fixed event fields, and every sorted unique revision ID, then yields fixed event and revision records. Event and revision JSONL line limits are explicit caller inputs. UTF-8 validation, canonical JSON measurement, and hashing use bounded chunks. Incomplete consumption fails closed without advancing ordinals; readers close on early exit. Returned records are read-only mappings, so callers cannot change fields after the event ID is computed. Legacy v1/v2 report logic is untouched.

The rule spec fixes UTC `+00:00` timestamps or null, Unicode code-point sorting without normalization, the NUL-terminated digest domain, 8-byte big-endian length prefixes, and caller-supplied resource bounds.

## Three independent QA roles

All three roles accepted final developer SHA `ed3399dfbbf9f6818ef256fd0d9913bfdc248075`:

- Full projection/Quality regression: `87 passed in 35.35s`; Ruff passed; format check reported `2 files already formatted`; mypy succeeded for 2 source files; diff-check clean.
- Boundary QA: `4 passed in 1.55s` on immutability, high-cardinality projection, early-close cleanup, and bounded stream round-trip. Earlier targeted boundary checks also passed exact/over byte caps, escaped UTF-8, Unicode ordering, and rejection before storage writes.
- Static ADR/rule review: ACCEPT. It confirmed record bounds, digest encoding, ordinals, UTC representation, RunSet limits, immutable output, writer compatibility, and unchanged legacy reporters.

## Integrated candidate verification

On the integrated candidate:

```text
uv run pytest -q tests/infrastructure/quality/test_report_projection.py tests/infrastructure/quality/test_report_streams.py tests/infrastructure/quality/test_reporter.py tests/infrastructure/quality/test_listing_report.py
87 passed in 35.36s

uv run ruff check infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
2 files already formatted

uv run mypy --follow-imports=silent infrastructure/quality/report_projection.py tests/infrastructure/quality/test_report_projection.py
Success: no issues found in 2 source files

git diff --check HEAD^ HEAD
```

The diff check exited 0 with no output.

After updating project status and memory, `uv run pytest -q tests/test_docs_consistency.py` returned `2 failed, 5 passed in 0.21s`. The failures refer to pre-existing unrelated documentation: ADR-0080's index status and the missing ADR-0074 link in ADR-0083. Neither file is part of this slice. `git diff --check` passed for the documentation changes.

## Remaining gates

- Connect the projection to the v3 canonical reporter while bounding existing report derivation and preserving legacy v1/v2 replay.
- Define and stream the evidence-gap projection, including its ADR-0031 completeness proof.
- Implement manifest commit/replay, finite snapshot-binding validation, and Dataset v3 integration.
- Measure complete process RSS on a main-matching clean SHA against the 32 MiB E1-CAP-1 threshold.

E1-CAP-1 remains unproven and Phase 1 remains open.
