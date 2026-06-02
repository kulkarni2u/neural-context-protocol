"""Cost-tracking middleware.

Estimates per-turn costs from raw string lengths and records them
to the store.  The primary cost-logging path remains in the assembler
(``log_cost`` with full ``NCPResponse`` data); this middleware provides
a lightweight parallel estimate for observability.
"""

from __future__ import annotations

from uuid import uuid4

from ncp.middleware.base import Middleware
from ncp.types import ConsciousBlock


class CostTrackingMiddleware(Middleware):
    """Records estimated cost telemetry via post_call.

    Uses a simple token heuristic (split-by-space) when the store
    does not support ``log_cost_raw``.
    """

    def __init__(self, store: object | None = None) -> None:
        self._store = store

    def post_call(self, response: str, conscious: ConsciousBlock) -> str | None:
        if self._store is None:
            return None
        input_tok = max(1, len((conscious.task + " " + conscious.slot).split()))
        output_tok = max(1, len(response.split()))
        cost_usd = (input_tok + output_tok) * 0.000001

        if hasattr(self._store, "log_cost_raw"):
            self._store.log_cost_raw(
                agent_id=conscious.agent_id,
                model=getattr(conscious, "model", "unknown"),
                input_tokens=input_tok,
                output_tokens=output_tok,
                cost_usd=cost_usd,
                pipeline_id=conscious.pipeline_id,
                turn_id=f"cost_est_{uuid4().hex[:16]}",
                latency_ms=0,
            )
        return None
