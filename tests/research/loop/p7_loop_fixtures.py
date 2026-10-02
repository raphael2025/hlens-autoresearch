"""Shared fixtures of the ADR-0103 P7 loop tests (not a test module; every number TEST ONLY).

The plan is ``negation(tsmom_bars@1.0.0)``: a strategy root the single-instrument loop can run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.domain.research import Hypothesis, HypothesisOrigin
from research.hypotheses.p7_binding import p7_plan_condition
from research.hypotheses.typed_plan import PlanLimits, TypedPlan, parse_plan_json
from research.hypotheses.typed_plan_compiler import (
    P7_OPERATOR_ALLOWLIST,
    CompiledPlan,
    P7ExecutionSwitch,
    compile_lowered_plan,
)
from research.hypotheses.typed_plan_resolver import resolve_direct_references
from research.strategies.library import library_entries
from tests.research.hypotheses.p7_fixtures import spec_input
from tests.research.loop import loop_fixtures as fx

ENABLED = P7ExecutionSwitch(enabled=True)
CREATED = datetime(2024, 1, 1, tzinfo=UTC)
LIMITS = PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=3)
TSMOM = library_entries()[0].candidate()


class Resolver:
    def resolve(self, ref: Any) -> Any:
        return TSMOM.spec if ref == TSMOM.spec.ref else None


def negation_plan() -> TypedPlan:
    payload = {
        "schema_version": "1.3.0",
        "root": "inverse",
        "nodes": [
            {
                "id": "inverse",
                "operator": "negation",
                "inputs": [spec_input(TSMOM.spec)],
                "parameters": {},
            }
        ],
    }
    return parse_plan_json(json.dumps(payload, separators=(",", ":")), limits=LIMITS)


def compiled_plan(plan: TypedPlan) -> CompiledPlan:
    return compile_lowered_plan(
        plan,
        resolution=resolve_direct_references(plan, resolver=Resolver()),
        created_at=CREATED,
        allowlist=dict(P7_OPERATOR_ALLOWLIST),
        switch=ENABLED,
    )


def plan_hypothesis(
    compiled: CompiledPlan,
    *,
    name: str = "h_p7_inverse",
    origin: HypothesisOrigin = HypothesisOrigin.COMBINATION,
    family: str = fx.FAMILY,
    conditions: tuple[str, ...] | None = None,
) -> Hypothesis:
    root = compiled.root.spec
    return Hypothesis(
        name=name,
        version="1.0.0",
        created_at=CREATED,
        family_id=family,
        statement="the negated TSMOM loses what TSMOM gains (TEST ONLY)",
        conditions=conditions
        or (f"strategy = {root.name}@{root.version}", p7_plan_condition(compiled.plan_hash)),
        expected_direction="higher",
        minimum_meaningful_effect="declared before running (test only)",
        origin=origin,
        origin_refs=(TSMOM.spec.ref,),
    )
