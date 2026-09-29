# Worker runtime composition — decision packet (2026-09-29)

**Status: DECIDED — ADR-0093 Accepted (2026-09-29).** The explicit trusted runtime factory was
selected. This decision does not change ADR-0044 / ADR-0049.

## Confirmed state

- WBS W2 requires API and Worker startup, shutdown, restart, and recovery evidence.
- `apps/worker` provides `JobRunner`, durable results, and generic loop machinery. ADR-0044 keeps
  the worker dependent on `core` and the standard library; ADR-0049 keeps research stages injected
  from the research-side composition root. `apps/worker` must not import `research/`.
- `tests/apps/worker_jobs_child.py` is explicitly test-only. It proves process recovery of the
  durable runner but is not a production executable.
- There is no production handler registry, Worker settings contract, or composition root. A process
  command cannot safely decide which jobs to register or whether to run generic jobs versus a
  research loop without making that choice explicit.

## Decision recorded

Choose the production composition boundary for the Worker process:

1. **Explicit trusted runtime factory (recommended for W2):** a production command receives an
   operator-supplied `module:callable` factory. The factory returns the already configured generic
   `JobRunner` / runtime; the Worker host only owns bounded polling, SIGINT/SIGTERM shutdown, and
   process exit. It never imports research code or derives handler registration from job data.
2. **Plugin-registered handlers:** add a named worker-handler plugin group and define how installed
   plugins are selected and initialized. This makes handler discovery part of the supported
   extension surface and requires a wider plugin/security decision.
3. **Defer the executable:** keep the test-only cross-process harness as evidence, record W2 partial,
   and leave production Worker launch for a later architecture batch.

ADR-0093 fixes the remaining runtime defaults: one job per poll, one-second idle wait, signal-driven
stop after an active job reaches the existing result/ack boundary, and supervisor-managed restart.

## Verification

- Launch the production command as a subprocess with an explicit test runtime factory and a
  file-backed bus/results journal.
- Submit one task, verify its persisted result, send SIGTERM and SIGINT in separate cases, and require
  clean exit after the current task reaches its existing acknowledgement boundary.
- Restart the same runtime and verify the completed task is not repeated and an interrupted task
  follows the existing `idempotent=` rule.
- Keep PostgreSQL + Iceberg restart evidence separately scoped; SQLite reopen is not a substitute.

Polling interval, batch limit, and shutdown-during-handler semantics must be fixed in the accepted
decision before implementing the command. No core contract, live trading capability, or research
stage policy needs to change for this bounded W2 task.

## Current disposition

The production host and focused subprocess tests are implemented on the isolated integration branch;
the selected host/job suites report `28 passed`, with Ruff and focused mypy clean. W2 remains open
pending broader API/Worker integration and PostgreSQL + Iceberg joint restart evidence. This is
independent of E1-CAP-1 and does not stop the other Phase 1 integration and validation work.
