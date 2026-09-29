# W10 Web runtime validation — 2026-09-29

## Scope

The Web application was checked in an isolated integration worktree based on the current W1 integration candidate. No frontend production code or dependency lockfile was changed. The only tracked changes in this slice update fixture-inventory assertions for newly committed report fixtures.

## Results

- Environment: Node `v26.8.1`, npm `11.19.0`; `npm ci` installed the lockfile dependencies without changing the lockfile.
- `npm run build`: passed; TypeScript checks and the Vite production build completed.
- `npm run test:components`: **120 passed, 0 failed**.
- The library suite first exposed several fixture-inventory assertions that no longer matched committed fixtures. Four exact fixture counts were updated to include the new fixtures while retaining the loops that parse every fixture and the legacy-version assertions.
- After those updates, `npm run test:lib` reported **110 passed, 1 failed**. The exact remaining case, `apps/web/src/lib/retroAudit.test.ts` — “the real fixture parses: report-only, no findings, named by its report_hash” — failed twice because it expects one fixture while the committed fixture directory contains two versions. Under the two-round policy it is deferred from this batch, remains unpassed, and is not retried here. A later fix should assert both versions explicitly and preserve the 1.0.0 legacy parse path.
- `npm audit` reported **3 advisories: 2 moderate and 1 high** across ECharts, Vite, and esbuild. Compatibility and remediation are under separate review; no automatic major-version upgrade was applied.

## Acceptance boundary

The successful build and component tests do not close W10. The Web library suite retains one deferred release gate, dependency advisories remain open, and the API/worker runtime evidence is tracked separately under W2. No Phase 1, W6, or W10 acceptance is claimed from this slice.
