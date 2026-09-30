# Research operations

One-shot local operations take all evidence as explicit inputs. They do not discover latest files,
read the clock, schedule work, or change lifecycle state. The ADR-0098 resolver (`authority.py`)
reads its authorities only at explicitly pinned identities (a Lifecycle Registry head, a v3 dataset
id + manifest hash) and runs one window backtest through the `BacktestProvider` protocol.

| Operation | Purpose | State |
|---|---|---|
| `degradation.py` | Validate explicit ACTIVE-history / PASS baseline / exact frozen Profile / freeze registry / recent metric manifest bindings, then recompute the ADR-0049 degradation check. The caller-supplied lifecycle is not claimed to be latest; observation truth is not independently verified. Report writing with evidence uses schema 1.1.0 through `research.reports.write_degradation_operation`. | `CODE_COMPLETE / DEBUG_PENDING`; no tests or Phase 11 acceptance yet |
| `degradation_cli.py` | One local invocation of the existing degradation operation and append-only report writer. Every artifact, UTC window, freeze registry + external anchor, and reports root is an explicit argument; it does not combine with a research loop. Two mutually exclusive modes (ADR-0098 §4): caller-declared `--lifecycle` + `--recent-manifest` (unchanged; stdout `evidence=caller-declared`) or `--authority-registry --authority-head <hash>\|latest --dataset-id --manifest-hash` (stdout `evidence=authority`). The plain command line refuses the authority mode with `authority_environment_unavailable` after verifying the pinned head: the catalog, decision pipeline and backtest provider can only come from an embedding caller's `AuthorityEnvironment`. | `CODE_COMPLETE / DEBUG_PENDING`; no tests or Phase 11 acceptance yet |
| `authority.py` | ADR-0098 resolver: `resolve_degradation_inputs` derives the lifecycle (Lifecycle Registry replay at an explicit head; subject must be in the ACTIVE set) and the recent metrics (closed `MONITORING_METRICS` registry calling the validation function of each baseline gate, over one window backtest of the pinned v3 manifest's proven bars) and returns an `AuthorityProvenance` that `run_degradation_check(..., authority=...)` writes as `evidence.authority`. Refusal codes include `metric_undefined`, `source_scope_mismatch`, `source_conflict`, `source_incomplete`. Only `breakeven_cost_multiple` (G4.cost_stress.breakeven) is defined. Not imported by the ADR-0074 operator or the API. | `CODE_COMPLETE / DEBUG_PENDING`; not run, no tests |

The operation deliberately refuses to run until the exact Profile has a verified ADR-0062 freeze
record and external anchor. An empty registry or missing observation source is not interpreted as
healthy. The CLI refuses missing registry files before opening the registry; opening an existing
registry takes its exclusive lock and may complete the registry's documented anchor-recovery step.
Keep the reports root separate from the freeze-registry root.
Use only one CLI process per reports root at a time: the report writer is append-only and
content-addressed but does not supply a cross-process directory lock.

Run `python -m research.operations.degradation_cli --help` for the required arguments and
`docs/plans/p11-degradation-operator-spec.md` for input JSON shapes and evidence boundaries.

Known gap (for the PM): ADR-0067 rule 5 requires a baseline gate's `metric` label to equal the
degradation metric key, but every exact-valued validation gate is written by `compare_gate` with a
comparator suffix (`breakeven_cost_multiple[>=]`), so `run_degradation_check` refuses any real
validation baseline on either path until that rule is decided.
