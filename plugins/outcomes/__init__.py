"""OutcomeProvider implementations (Phase 4, ADR-0037). Outcomes are labels, never inputs."""

from plugins.outcomes.forward_return import ForwardReturnOutcome
from plugins.outcomes.triple_barrier import TripleBarrierOutcome
from plugins.outcomes.vol_scaled_triple_barrier import VolScaledTripleBarrierOutcome

__all__ = ["ForwardReturnOutcome", "TripleBarrierOutcome", "VolScaledTripleBarrierOutcome"]
