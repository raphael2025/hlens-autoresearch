# ADR-0096: Read-only results for exact TrialLedger duplicates

| Field | Value |
|---|---|
| Status | **Accepted** |
| Date | 2026-09-30 |
| Decision maker | Codex, under the active project Engineering Director authorization |
| Related work | Phase 7 / durable research loop |
| Scope | `TrialLedger.register` and `register_reevaluation`; no contract, schema, Constitution, Profile, or experiment semantics change |

## Context

ADR-0073 makes new registrations and re-evaluations durable writes subject to the durable loop's admission gate. Its §3.2 also defines an exact duplicate `register_batch` as idempotent: it adds no journal entry and does not count as a new trial. An exact duplicate single registration or already-recorded attempt currently reaches the write gate first, however. After a round closes, during an admission lease, or after the state is closed, a caller retrying a request that already committed can receive a gate refusal even though acknowledging the duplicate requires no write.

The ledger must preserve its existing global lock order (`gate → ledger lock → journal lock`) and the human-review boundary for LLM-originated hypotheses.

## Decision

1. An exact duplicate `register(hypothesis)` is a read-only idempotent result and returns `False` without entering the write gate, appending to the journal, or changing trial counts. A match requires the same `(name, version)` and identical content hash.
2. An exact duplicate `register_reevaluation(hypothesis, attempt)` is likewise read-only and returns `False` only when identity, content hash, and the same normalized attempt key are already recorded. A fresh attempt remains a new trial and must pass the write gate.
3. The lookup takes the ledger lock only for the read and releases it before attempting the write gate. If the lookup finds no exact duplicate, the existing gated write path rechecks state while holding the gate and ledger lock. This preserves lock order and handles races.
4. The LLM-origin check in `register` remains before duplicate lookup. An LLM-origin hypothesis is always rejected by `register`, including when its identity and content match an existing entry; only the reviewed-draft API may register it.
5. Exact read-only acknowledgements may succeed after a round has closed, between rounds, while an admission lease is active, or after the durable state has closed. They are not new registration authority: every mutation, changed-content identity, new attempt, batch admission, recovery registration, and review operation remains subject to its existing gate and policy.
6. Tests must prove that each allowed duplicate leaves the journal head and trial count unchanged, and that fresh writes remain refused in those same gate states. A duplicate acknowledgement is not an experiment execution and cannot authorize rerunning a trial.

## Consequences

- Callers can safely retry a completed exact registration after losing its response without creating a new trial or receiving a misleading write-gate error.
- The write gate remains the exclusive authority for mutations. Documentation must distinguish read-only duplicate acknowledgement from ordinary writes.
- A closed gate does not become writable; new registrations and re-evaluation attempts still fail closed.

## Required implementation checks

- Keep the LLM-origin guard before the duplicate fast path.
- Cover exact and changed-content registrations, same and fresh re-evaluation attempts, closed-round/closed-state behavior, active-lease behavior, journal position, and trial count.
- Update TrialLedger, gate, and durable-loop documentation in the same change.
- Preserve strict recovery and batch-admission checks from ADR-0073.

## Implementation status

Implemented and reviewed on the isolated consolidation line on 2026-09-30. The ledger suite passed (`12 passed`); the durable/loop focused suite passed (`89 passed`) after correcting a collection-time missing import; a real `DurableState` closed-gate duplicate test passed (`1 passed`). Ruff, formatting, and targeted MyPy checks passed. Independent review: **APPROVE**. Full branch-line regression and Phase acceptance remain open. See [implementation review](../reviews/2026-09-30-adr-0096-ledger-idempotency.md).

## References

- [ADR-0073: P7 typed-plan pre-registration and crash recovery](0073-phase7-plan-admission-recovery.md)
- `research/persistence/gate.py`
- `research/loop/durable.py`
