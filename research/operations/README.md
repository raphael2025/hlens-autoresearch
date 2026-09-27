# Research operations

One-shot local operations take all evidence as explicit inputs. They do not discover latest files,
read the clock, run a provider, schedule work, or change lifecycle state.

| Operation | Purpose | State |
|---|---|---|
| `degradation.py` | Validate explicit ACTIVE-history / PASS baseline / exact frozen Profile / freeze registry / recent metric manifest bindings, then recompute the ADR-0049 degradation check. The caller-supplied lifecycle is not claimed to be latest; observation truth is not independently verified. Report writing with evidence uses schema 1.1.0 through `research.reports.write_degradation_operation`. | `CODE_COMPLETE / DEBUG_PENDING`; no tests or Phase 11 acceptance yet |
| `degradation_cli.py` | One local invocation of the existing degradation operation and append-only report writer. Every artifact, UTC window, freeze registry + external anchor, and reports root is an explicit argument; it does not combine with a research loop. | `CODE_COMPLETE / DEBUG_PENDING`; no tests or Phase 11 acceptance yet |

The operation deliberately refuses to run until the exact Profile has a verified ADR-0062 freeze
record and external anchor. An empty registry or missing observation source is not interpreted as
healthy. The CLI refuses missing registry files before opening the registry; opening an existing
registry takes its exclusive lock and may complete the registry's documented anchor-recovery step.
Keep the reports root separate from the freeze-registry root.
Use only one CLI process per reports root at a time: the report writer is append-only and
content-addressed but does not supply a cross-process directory lock.

Run `python -m research.operations.degradation_cli --help` for the required arguments and
`docs/plans/p11-degradation-operator-spec.md` for input JSON shapes and evidence boundaries.
