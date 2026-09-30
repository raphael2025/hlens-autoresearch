# ADR-0096 TrialLedger idempotency slice review

**Date:** 2026-09-30  
**Integration line:** `codex/project-consolidation`  
**Implementation commits:** `5c016b8`, `d65a17a`, `807c6eb`  
**Decision:** Slice accepted; this is not Phase 7 or Phase 1 acceptance.

## Scope reviewed

Exact duplicate `TrialLedger.register` calls and repeated normalized `register_reevaluation` attempts return `False` as read-only acknowledgements. The LLM-origin guard remains before the duplicate fast path. New identities, changed content, and new attempts still enter the existing admission gate. The implementation keeps the gate → ledger → journal lock order.

The integration also fixes the missing `contextmanager` import in `infrastructure/dataset/builder.py`, exposed when collecting the loop suite. No experiment, schema, or frozen contract semantics changed.

## Verification evidence

Commands were run in the consolidation worktree:

| Command / check | Result |
|---|---|
| `uv run pytest tests/research/hypotheses/test_durable_ledger.py` | `12 passed in 0.30s` |
| `uv run pytest tests/research/loop/test_loop_durable.py tests/research/loop/test_loop_retry_admission.py tests/research/loop/test_retry_admission.py` | First attempt: exit 2, `0 collected`, 3 collection errors because `contextmanager` was not imported in `infrastructure/dataset/builder.py`. After fixing the import, the same command: `89 passed in 163.17s`. |
| `uv run pytest tests/research/loop/test_loop_durable.py::test_exact_registration_duplicate_is_read_only_through_durable_gate` | `1 passed in 6.02s` |
| `uv run ruff check research/hypotheses/ledger.py infrastructure/dataset/builder.py tests/research/hypotheses/test_durable_ledger.py tests/research/loop/test_loop_durable.py` | `All checks passed!` |
| `uv run ruff format --check research/hypotheses/ledger.py infrastructure/dataset/builder.py tests/research/hypotheses/test_durable_ledger.py tests/research/loop/test_loop_durable.py` | `4 files already formatted` |
| `uv run mypy research/hypotheses/ledger.py infrastructure/dataset/builder.py` | `Success: no issues found in 2 source files` |

The initial Ruff run identified two 101-character lines in `research/hypotheses/ledger.py`; both were wrapped, then Ruff and format checks passed. `git diff --check` passed before commit.

## Independent review

The independent ledger reviewer initially requested a blocker-level integration test because a generic closed-gate fake did not prove the real `DurableState` behavior. A dedicated durable-gate test was added and passed. The reviewer then returned **APPROVE**.

## Acceptance boundary

This evidence accepts only the ADR-0096 implementation slice. No full consolidation-tree suite or E1-CAP-1 capacity run was performed. Phase 7, E1-CAP-1, and Phase 1 remain open. The original dirty W1/E1 worktrees remain preserved.
