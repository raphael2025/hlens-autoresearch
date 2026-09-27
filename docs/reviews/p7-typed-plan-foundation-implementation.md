# P7 typed plan foundation — implementation note

**Scope:** the non-runnable parser and structural type checker in
`research/hypotheses/typed_plan.py`, implementing the closed-world mechanism authorized by
ADR-0068. No DSL operator is enabled.

## API

- `PlanLimits(max_depth, max_nodes, max_json_bytes, max_parameters_per_node)` is required by
  `parse_plan_json`; every limit must be an exact positive integer. Booleans do not pass as ints.
- `parse_plan_json(raw, limits=...) -> TypedPlan` accepts one UTF-8 JSON document in this shape:

  ```json
  {
    "schema_version": "1.0.0",
    "root": "n1",
    "nodes": [
      {
        "id": "n1",
        "operator": "interaction",
        "inputs": [
          {"ref": "feature:first@1.0.0", "content_hash": "<64 lowercase hex>"},
          {"ref": "feature:second@1.0.0", "content_hash": "<64 lowercase hex>"}
        ],
        "parameters": {}
      }
    ]
  }
  ```

  A node may instead consume an earlier node with `{"node": "prior_id"}`. Node arrays are
  topologically ordered; all nodes must be reachable from `root`. Input order is preserved.
- `TypedPlan.content_hash()` hashes the canonical plan payload using the repository's
  `core.domain.base.content_hash`. It includes the plan format, ordered nodes, resolved reference
  strings and caller-supplied hashes, parameters, and explicit resource limits. It is a structural
  fingerprint, not an execution audit record or proof that a supplied reference/hash exists.
- `compile_plan(plan)` always raises `PlanRefused`: this module contains no implementation
  allowlist, lowering, or runnable entry point. `PlanNode.runnable` and `TypedPlan.runnable` are
  always `False`.

## Structural checks and refusals

The parser rejects unknown root/node/input/parameter fields and operators; duplicate JSON keys;
invalid UTF-8 / JSON; floating-point and non-standard JSON constants; invalid reference syntax or
SHA-256 shape; duplicate node IDs; forward, missing, or unreachable node references; incorrect
operator input arity/kind; implicit input-kind conversions; parameter type coercion; unsupported
transform names; malformed operator parameter shapes; and caller limit violations.

Operator input/output type signatures follow the nominal table in ADR-0068. The accepted
declaration helpers supply the transformation-name set; the AST parameter keys are only syntax.
They do not define transformation algorithms, temporal calendars, provider behavior, or any other
missing operator semantics. `Ref` and its hash are not looked up in a Registry here.

## Explicitly out of scope

No Provider, Registry, `ExperimentSpec`, `TrialLedger`, persistence, loop integration, batch
preregistration, tests, or operator lowering changed. All six operators remain non-runnable.
Execution audit persistence and atomic TrialLedger preregistration must be resolved before any
operator is enabled, as stated by the coordinator's task instruction.

## Checks

No tests or acceptance checks were run, per task instruction. Static checks on
`research/hypotheses/typed_plan.py`:

- `uv run --offline ruff check research/hypotheses/typed_plan.py` — passed.
- `uv run --offline ruff format --check research/hypotheses/typed_plan.py` — already formatted.
- `uv run --offline mypy research/hypotheses/typed_plan.py` — `Success: no issues found in 1 source file`.
- `git diff --check` — passed before commit.
