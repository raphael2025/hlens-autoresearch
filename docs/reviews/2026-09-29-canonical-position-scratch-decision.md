# DECISION PACKET: Canonical position-index scratch ownership

## State

`ARCHITECTURE_DECISION_REQUIRED` for implementation ownership and configuration. This packet does
not approve a design or change E1-CAP-1 status. No code or tests were changed/run.

## Approved scope checked

- `PROJECT_STATUS.md` §1, §4, §5, §6, and §12 keep Phase 1 open and E1-CAP-1 as its capacity
  blocker. D-E1-CAP-ARCH authorizes E1-R infrastructure slices against the current `main` baseline;
  the complete 32 MiB process-workset gate remains in force.
- ADR-0075 §8 explicitly leaves temporary-directory placement in the E1 scope. Its scan rule does
  not grant a storage lifecycle or a configured scratch root.
- ADR-0076 requires explicit stream resource ownership and cleanup, but covers the normalizer result
  API rather than defining the position-index scratch provider.
- ADR-0077 §3.5 / DQ-11 defines immutable content-addressed storage for persistent evidence objects;
  it does not grant a delete operation or define ephemeral scratch ownership. Reusing it for
  temporary position files would leave permanent objects after each call.
- `core/contracts/storage.py::StorageAdapter` is frozen and only exposes stage/publish/lookup/read.
  This task must not add scratch/delete methods to it.

## Verified implementation facts

In `infrastructure/canonical/normalizer.py`, `_PositionIndex`:

- creates a `tempfile.TemporaryDirectory(prefix="hlens-positions-")` with no explicit parent, so
  path selection follows the process-wide Python temp-directory rules;
- stores each input position in a SQLite table, caps SQLite's page cache at 1 MiB, and writes the
  sorted positions to a fixed-width rank file during finalization;
- exposes lazy rank reads and closes the SQLite connection, file descriptor, and temporary
  directory on close/destruction.

This establishes that Python position tuples were removed from the long-lived survey/index API.
It does not establish that the scratch filesystem is outside cgroup memory (the project's default
`/tmp` is documented as tmpfs), that disk usage has a configured quota, or that cleanup is timely
on all process termination paths. The E1 probe redirects `TMPDIR` to its per-run directory, but
normal production callers do not provide a scratch path.

## Question

Who owns the explicit, non-default scratch root and its capacity/cleanup policy for normalizer
position indexes?

## Options

### A. Inject an infrastructure scratch root (recommended)

Add a private `CanonicalScratch`/path dependency to `CanonicalNormalizer`; require callers to
provide it, create each index under that root, and fail closed if the root is absent or unusable.
The composition roots, E1 probe, and test harnesses then pass their configured work/scratch paths.
Keep `StorageAdapter` unchanged.

- Pros: explicit location and ownership; temporary objects remain ephemeral; tests can prove all
  created files are under the supplied root and removed after close/failure.
- Costs: requires coordinated edits in existing call sites under `infrastructure/pit/` and tools,
  plus a runtime configuration source and a documented scratch-space exhaustion policy. Those
  files are outside the current canonical-only implementation scope.

### B. Use a dedicated private scratch provider

Introduce an infrastructure-only scratch provider with create/open/close/delete operations and
explicit filesystem/capacity policy, independent of `StorageAdapter`.

- Pros: clear lifecycle boundary and future reuse for parser / other spill paths.
- Costs: new cross-module abstraction and configuration; larger design/review surface than this
  canonical slice.

### C. Keep relying on `TMPDIR`

Require the runtime/operator to set `TMPDIR`, while retaining Python's default temp resolution.

- Pros: small code change and compatible with the existing probe behavior.
- Costs: normalizer itself cannot distinguish an intentional scratch root from a system default;
  production path safety depends on external process configuration and is not enforced by the
  API. This does not close the identified path-ownership gap.

### D. Store scratch through `StorageAdapter`

Use content-addressed immutable objects for each position run.

- Rejected recommendation: the contract has no delete/temporary-object lifecycle, so each
  normalization would leave persistent orphan objects. Adding delete semantics to the frozen
  contract is outside this scope.

## Recommendation

Choose **A** for the next implementation slice. First authorize the small, explicit cross-module
composition change and identify the setting/path owner. Keep scratch byte quotas as an explicit
follow-up unless E1 measurements establish a required bound; do not claim bounded total disk use
from the fixed SQLite cache. The existing 32 MiB memory gate and all normalizer validation rules
remain unchanged.

## Acceptance criteria after a decision

1. No normalizer position-index path resolves to the process default temp directory implicitly.
2. The configured scratch root is validated before any Raw scan or write; missing/unusable root
   fails closed before side effects.
3. SQLite database, SQLite sort temporaries, and rank file all use a documented filesystem. The
   implementation must account for SQLite's temporary-file placement, not only the database path.
4. Index close, early iterator close, read/write exceptions, and normalizer cache eviction release
   connections/descriptors and remove per-index files; tests assert directory contents and no
   retained references.
5. Structural tests prove no Python container grows with unit positions; disk usage is reported as
   O(N) spill and is not misrepresented as a hard byte bound.
6. Run canonical non-PostgreSQL focused tests, lint and type checks; later run the approved isolated
   E1 memory matrix on the current code line. These checks remain separate from PostgreSQL tests.

## Commands and observed output

Commands run read-only against the isolated `main` worktree:

```text
git worktree add -b codex/canonical-position-bounds \
  /home/raphael/.codex/worktrees/canonical-position-bounds/hlens-autoresearch main
```

Output:

```text
Preparing worktree (new branch 'codex/canonical-position-bounds')
HEAD is now at e584187 test(state): repair persistence regression fixtures
```

```text
rg -n 'class StorageAdapter|TemporaryDirectory|tempfile' \
  core infrastructure/canonical tests/infrastructure/canonical
```

Relevant output:

```text
infrastructure/canonical/normalizer.py:49:import tempfile
infrastructure/canonical/normalizer.py:184:        self._temporary = tempfile.TemporaryDirectory(prefix="hlens-positions-")
core/contracts/storage.py:228:class StorageAdapter(Protocol):
```

```text
rg -n 'CanonicalNormalizer\(' --glob '*.py'
```

This found direct construction in canonical tests, `infrastructure/tools/` and
`infrastructure/pit/selector.py`; supplying an explicit root cannot be completed inside the
canonical module alone.

No pytest / database / probe / lint / typecheck command was run. The root checkout's PostgreSQL
full-suite process was not touched.

## Handoff

- Files changed: this Decision Packet only.
- Phase / criterion: Phase 1 E1-CAP-1; no acceptance criterion is marked passed by this packet.
- ADRs: reviewed ADR-0075, ADR-0076, ADR-0077; no ADR amended.
- Reviewer / integration: not reviewed; not integrated or merged to `main`.
- Unresolved: approve A or B and assign the runtime scratch-path owner. Until then, preserve current
  behavior as an open E1 risk and do not report Phase 1 capacity as passing.
