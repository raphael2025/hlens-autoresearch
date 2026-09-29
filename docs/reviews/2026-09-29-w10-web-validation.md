# W10 Web runtime validation — 2026-09-29

## Scope

The Web application was checked in an isolated integration worktree based on the current W1 integration candidate. No frontend production code or dependency lockfile was changed. The only tracked changes in this slice update fixture-inventory assertions for newly committed report fixtures.

## Results

- Environment: Node `v26.8.1`, npm `11.19.0`; `npm ci` installed the lockfile dependencies without changing the lockfile.
- `npm run build`: passed; TypeScript checks and the Vite production build completed.
- `npm run test:components`: **120 passed, 0 failed**.
- The library suite first exposed several fixture-inventory assertions that no longer matched committed fixtures. Four exact fixture counts were updated to include the new fixtures while retaining the loops that parse every fixture and the legacy-version assertions.
- After those updates, `npm run test:lib` reported **110 passed, 1 failed**. The exact remaining case, `apps/web/src/lib/retroAudit.test.ts` — “the real fixture parses: report-only, no findings, named by its report_hash” — failed twice because it expects one fixture while the committed fixture directory contains two versions. Under the two-round policy it is deferred from this batch, remains unpassed, and is not retried here. A later fix should assert both versions explicitly and preserve the 1.0.0 legacy parse path.
- The initial `npm audit` reported **3 advisories: 2 moderate and 1 high** across ECharts, Vite, and esbuild. The isolated security change `codex/web-dependency-security@5070d17` upgrades only the manifest and lockfile to ECharts 6.1.0, Vite 6.4.3, and esbuild 0.25.12; it is now cherry-picked into the review candidate `codex/w1-independent-integration@885cdf5`. On the security branch, `npm ci`, production build, component tests (120/120), and `npm audit` passed with **0 vulnerabilities**; an independent review approved the lockfile and peer ranges. The integrated candidate's clean install, build, components, and audit were also verified below.
- ECharts is a major-version change. Its line, bar, and heatmap pages have build/component coverage, but visual rendering and interaction have not been inspected.

## Acceptance boundary

The integrated candidate clean install completed with **0 vulnerabilities**; production build and component tests passed (`120 passed`) and `npm audit` reported **0 vulnerabilities**. The successful build and component tests do not close W10. The Web library suite retains one deferred release gate and ECharts visual review remains open. API/worker runtime evidence is tracked separately under W2. No Phase 1, W6, or W10 acceptance is claimed from this slice.
