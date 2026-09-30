# E1 revision ID iterator test hardening

**Result: ACCEPT for the test-only maintenance slice.** No production implementation, contract, or ADR changed.

## Scope

The isolated change was developed on `codex/e1-revision-ids-tests` from integration candidate `894c47e`, then integrated to `codex/e1-phase1-progress@cb5d6b7`. It changes only `tests/infrastructure/canonical/test_normalizer.py`.

The change adds two direct checks: a tampered `CanonicalUnitNormalized.revision_count` is rejected, and explicitly closing the ID iterator closes all retained disk-backed position indexes. It fixes four existing test helper calls that omitted required `clock=`, repairs an undefined `n` in recovery parity, updates `_proof_windows` expectations to include the actual yielded count, and removes an unused import. Assertions were preserved or strengthened.

## Test history and independent QA

First full-file run, before repairing the test defects:

```text
uv run pytest -q tests/infrastructure/canonical/test_normalizer.py
5 failed, 73 passed in 39.55s
```

Four recovery parameters failed with `NameError: n is not defined`; one proof-window expectation omitted the yielded count. These were test-file defects and were repaired. The one permitted follow-up full-file run passed:

```text
uv run pytest -q tests/infrastructure/canonical/test_normalizer.py
78 passed in 40.14s
```

The focused developer selection passed `11 passed in 7.63s`; the integrated candidate selection passed `11 passed in 7.73s`. Three independent QA roles accepted the test-only change; one independently ran 10 focused nodes (`10 passed in 7.39s`), another ran the two new lifecycle/tampering nodes (`2 passed in 1.52s`), and the third confirmed the expected tuple semantics and absence of weakened assertions.

Ruff: `All checks passed!`; format: `1 file already formatted`; `git diff --check` clean. Mypy still reports 14 existing diagnostics across unchanged infrastructure modules and unchanged test lines. No third full-file run was made.

The iterator API boundary now has direct regression coverage for summary tampering and early-close cleanup. Full-process memory remains unmeasured; this does not pass E1-CAP-1 or Phase 1.
