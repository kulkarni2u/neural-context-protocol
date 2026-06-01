"""Public package surface for Neural Context Protocol."""

from .api import agent, configure, emit, get_context, run, stream, write_memory
from .benchmarks import estimate_tokens, run_coding_pipeline_benchmark, run_research_pipeline_benchmark, token_unit
from .dogfood import (
    get_live_provider_readiness,
    load_dogfood_adapter,
    run_adapter_continuation_dogfood_loop,
    run_canonical_dogfood_loop,
    run_canonical_http_dogfood_loop,
    run_live_adapter_continuation_attempt,
    run_repeatability_dogfood_loop,
)
from .version import __version__

__all__ = [
    "__version__",
    "agent",
    "configure",
    "estimate_tokens",
    "emit",
    "get_live_provider_readiness",
    "get_context",
    "load_dogfood_adapter",
    "run_adapter_continuation_dogfood_loop",
    "run_canonical_dogfood_loop",
    "run_canonical_http_dogfood_loop",
    "run_live_adapter_continuation_attempt",
    "run_repeatability_dogfood_loop",
    "run",
    "run_coding_pipeline_benchmark",
    "run_research_pipeline_benchmark",
    "stream",
    "token_unit",
    "write_memory",
]
