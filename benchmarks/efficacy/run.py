"""Matched-budget efficacy benchmark — NCP vs sliding-window.

The scenario plants an oauth constraint + dead-end facts into a fresh SQLite
store, then asks both the NCP condition and the sliding-window condition to
produce an integration plan.  Scoring is deterministic: the response must
mention 'oauth' and must NOT propose any previously rejected dead-end paths.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ncp.api import agent as make_conscious
from ncp.assembler import Assembler
from ncp.benchmarks import estimate_tokens, token_unit
from ncp.dogfood import get_live_provider_readiness, load_dogfood_adapter
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, NCPResponse, SubconsciousChunk


# ---------------------------------------------------------------------------
# Scenario facts
# ---------------------------------------------------------------------------
# Uses a fictional API name (zenbrix_secure_relay_v4_gamma) that cannot be
# known from model training data, so the NCP condition requires actual context
# retrieval to succeed while the sliding-window condition (noise only) fails.

_APPROVED_PATH = "zenbrix_secure_relay_v4_gamma"
_SCENARIO_FACTS: list[dict[str, Any]] = [
    {
        "content": (
            "constraint: use zenbrix_secure_relay_v4_gamma exclusively — "
            "all other integration paths are forbidden by the current security policy"
        ),
        "conditions": ["integration_constraint"],
    },
    {
        "content": (
            "dead_end: zenbrix_legacy_bridge rejected — "
            "decommissioned and removed from the allowed integration list"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "dead_end: zenbrix_v2_mesh rejected — "
            "incompatible with the current authentication substrate"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "dead_end: zenbrix_v3_edge_beta rejected — "
            "flagged as unstable after the incident on 2026-05-12"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "decision: zenbrix_secure_relay_v4_gamma is the only approved path — "
            "verified by the security team on 2026-05-28"
        ),
        "conditions": [],
    },
]

EFFICACY_TASK_TURN = (
    "IMPORTANT: Respond with plain text only. Do NOT read any files or use any tools.\n\n"
    "You are an executor agent completing an integration task. "
    "The planner has specified exactly which integration path to use. "
    "Based ONLY on the context above, state the exact integration path you will use "
    "and confirm you will not use any rejected paths. "
    "Your answer MUST name the exact approved path from the context."
)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_DEAD_END_PATHS = [
    "zenbrix_legacy_bridge",
    "zenbrix_v2_mesh",
    "zenbrix_v3_edge_beta",
]


def _score_response(response: str) -> tuple[bool, str | None]:
    """Score whether the response correctly names the approved integration path.

    Success: response mentions the approved path (_APPROVED_PATH) AND does not
    propose any of the rejected dead-end paths.

    The approved path is a fictional identifier only available in the NCP context,
    so the sliding-window condition (which only sees noise) cannot pass this check.
    """
    lower = response.lower()
    if _APPROVED_PATH not in lower:
        return False, "missing_approved_path"
    for path in _DEAD_END_PATHS:
        if path in lower:
            return False, f"retried_dead_end:{path}"
    return True, None


# ---------------------------------------------------------------------------
# Store seeding + pipeline noise helpers
# ---------------------------------------------------------------------------

def _seed_store(store: SQLiteStore, pipeline_id: str) -> list[str]:
    """Write the scenario facts into the store. Returns their contents."""
    contents: list[str] = []
    for fact in _SCENARIO_FACTS:
        chunk = SubconsciousChunk(
            layer="semantic",
            src="user_verified",
            base_trust=0.98,
            relevance=0.95,
            content=fact["content"],
            conditions=fact["conditions"],
            pipeline_id=pipeline_id,
            written_by="bench_seed",
        )
        store.write(chunk)
        contents.append(fact["content"])
    return contents


def _add_pipeline_noise(
    store: SQLiteStore,
    pipeline_id: str,
    n_turns: int = 20,
) -> list[str]:
    """Write filler chunks AFTER the constraint facts to simulate pipeline depth.

    This pushes the constraint facts into the past so a recency-based sliding
    window only sees noise, while NCP's query-based retrieval still finds the
    high-relevance constraint chunks.

    Returns the filler content strings in write order.
    """
    noise: list[str] = []
    for i in range(1, n_turns + 1):
        # Each entry is ~50 words; 20 entries ≈ 1000 words — well above a 600-token
        # budget so the sliding window can only hold noise, never constraint facts.
        content = (
            f"turn {i:02d}: routine implementation progress — "
            "the executor is validating endpoint configuration, verifying TLS certificate "
            "bindings, checking exponential-backoff retry logic, and confirming the "
            "service layer dispatch chain; the planner has not issued new constraints "
            "this turn; all checks nominal; proceeding to the next pipeline stage"
        )
        chunk = SubconsciousChunk(
            layer="episodic",
            src="agent_inferred",
            base_trust=0.4,
            relevance=0.05,
            content=content,
            pipeline_id=pipeline_id,
            written_by="bench_noise",
        )
        store.write(chunk)
        noise.append(content)
    return noise


def _build_window_context(transcript: list[str], budget: int) -> str:
    """Build a sliding-window context from the MOST RECENT transcript entries.

    Fills from the end of the transcript backwards until the budget (in tokens)
    is reached. With constraints first and noise last in the transcript, a tight
    budget returns only noise — no constraint information.
    """
    selected: list[str] = []
    used = 0
    for entry in reversed(transcript):
        tokens = estimate_tokens(entry)
        if used + tokens > budget:
            break
        selected.append(entry)
        used += tokens
    return "\n".join(reversed(selected)) if selected else ""


# ---------------------------------------------------------------------------
# NCP condition
# ---------------------------------------------------------------------------

def _run_ncp_attempt(
    adapter: Any,
    store_path: Path,
    pipeline_id: str,
    budget: int,
) -> dict[str, object]:
    """Run one NCP-condition attempt.

    Seeds constraint facts THEN adds pipeline noise so NCP must retrieve
    constraint chunks through the noise via query-based relevance scoring.
    """
    store = SQLiteStore(store_path)
    _seed_store(store, pipeline_id)
    _add_pipeline_noise(store, pipeline_id)   # push constraints into past

    assembler = Assembler(store=store)
    conscious = make_conscious(
        id="bench_executor",
        role="pravaha",
        owns=["integration"],
        must_not=["rejected_paths"],
        task="oauth_integration_plan",
        slot="build",
        intent="oauth_pkce_integration",
        pipeline_id=pipeline_id,
    )
    budget_ctx = BudgetContext(
        ctx_used=0.7,    # late-pipeline pressure so assembler works hard to retrieve
        pressure="medium",
    )
    assembly = assembler.assemble(
        conscious=conscious,
        budget=budget_ctx,
        query_text="oauth constraint forbidden paths integration plan",
        k=4,
    )

    # Combine context + task into user_turn so all adapters (including ClaudeCLI,
    # which ignores ncp_context in its fetch-inject design) receive the full prompt.
    full_turn = f"[NCP assembled context]\n{assembly.context}\n\n[Task]\n{EFFICACY_TASK_TURN}"
    prompt_tokens = estimate_tokens(full_turn)

    try:
        response_text = adapter.call(
            ncp_context="",
            user_turn=full_turn,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "prompt_tokens": prompt_tokens,
            "failure_type": "timeout",
            "response": "",
        }
    except RuntimeError as exc:
        return {
            "success": False,
            "prompt_tokens": prompt_tokens,
            "failure_type": f"adapter_error:{str(exc)[:80]}",
            "response": "",
        }

    # post_turn (contract compliance)
    ncp_response = NCPResponse(
        content=response_text,
        input_tokens=prompt_tokens,
        output_tokens=estimate_tokens(response_text),
        cost_usd=0.0,
        model="bench_ncp",
        pipeline_id=pipeline_id,
        turn_id=f"ncp_{int(time.time() * 1000)}",
        latency_ms=1,
    )
    assembler.post_turn(
        conscious=conscious,
        response=ncp_response,
        result_summary=response_text.splitlines()[0] if response_text else "",
        result_full=response_text,
    )

    success, failure_type = _score_response(response_text)
    return {
        "success": success,
        "prompt_tokens": prompt_tokens,
        "failure_type": failure_type,
        "response": response_text,
    }


# ---------------------------------------------------------------------------
# Sliding-window condition
# ---------------------------------------------------------------------------

def _run_sliding_window_attempt(
    adapter: Any,
    store_path: Path,
    pipeline_id: str,
    budget: int,
) -> dict[str, object]:
    """Run one sliding-window condition attempt.

    Seeds the same constraint facts then adds the same pipeline noise, but
    builds context from the MOST RECENT transcript entries (the noise), not
    from the full fact list. At budget=600 the window fills with noise turns;
    constraint facts planted earlier fall outside the window.
    """
    store = SQLiteStore(store_path)
    fact_contents = _seed_store(store, pipeline_id)
    noise_contents = _add_pipeline_noise(store, pipeline_id)
    transcript = fact_contents + noise_contents   # constraints first, noise last

    sliding_window_context = _build_window_context(transcript, budget)

    full_turn = f"[Sliding-window context]\n{sliding_window_context}\n\n[Task]\n{EFFICACY_TASK_TURN}"
    prompt_tokens = estimate_tokens(full_turn)

    try:
        response_text = adapter.call(
            ncp_context="",
            user_turn=full_turn,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "prompt_tokens": prompt_tokens,
            "failure_type": "timeout",
            "response": "",
        }
    except RuntimeError as exc:
        return {
            "success": False,
            "prompt_tokens": prompt_tokens,
            "failure_type": f"adapter_error:{str(exc)[:80]}",
            "response": "",
        }

    success, failure_type = _score_response(response_text)
    return {
        "success": success,
        "prompt_tokens": prompt_tokens,
        "failure_type": failure_type,
        "response": response_text,
    }


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def _summarize(attempts: list[dict[str, object]]) -> dict[str, object]:
    if not attempts:
        return {"success_rate": 0.0, "median_prompt_tokens": 0.0, "timeout_rate": 0.0}
    successes = sum(1 for a in attempts if a["success"])
    timeouts = sum(1 for a in attempts if a.get("failure_type") == "timeout")
    tokens = [int(a["prompt_tokens"]) for a in attempts]
    return {
        "success_rate": successes / len(attempts),
        "median_prompt_tokens": float(statistics.median(tokens)) if tokens else 0.0,
        "timeout_rate": timeouts / len(attempts),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_efficacy(
    *,
    continuation_adapter: str,
    budget: int = 600,
    attempts: int = 1,
    adapter_timeout_seconds: float | None = None,
    pipeline_id: str = "bench_efficacy",
    cwd: str | Path | None = None,
) -> dict[str, object]:
    """Run matched-budget efficacy benchmark for one adapter.

    Returns a structured artifact with NCP and sliding-window conditions,
    per-attempt detail, and summary statistics.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    ncp_attempts: list[dict[str, object]] = []
    sw_attempts: list[dict[str, object]] = []

    for attempt_index in range(1, attempts + 1):
        attempt_pipeline_id = f"{pipeline_id}_attempt_{attempt_index}"

        # Check readiness (advisory only — we still attempt).
        get_live_provider_readiness(continuation_adapter)

        # Load adapter.
        adapter = load_dogfood_adapter(
            continuation_adapter,
            timeout_seconds=adapter_timeout_seconds,
        )

        # NCP condition — fresh tmpdir store.
        with tempfile.TemporaryDirectory() as ncp_tmp:
            ncp_store_path = Path(ncp_tmp) / "ncp.db"
            ncp_result = _run_ncp_attempt(
                adapter=adapter,
                store_path=ncp_store_path,
                pipeline_id=f"{attempt_pipeline_id}_ncp",
                budget=budget,
            )
            ncp_result["attempt"] = attempt_index
            ncp_attempts.append(ncp_result)

        # Sliding-window condition — separate fresh tmpdir store.
        with tempfile.TemporaryDirectory() as sw_tmp:
            sw_store_path = Path(sw_tmp) / "sw.db"
            sw_result = _run_sliding_window_attempt(
                adapter=adapter,
                store_path=sw_store_path,
                pipeline_id=f"{attempt_pipeline_id}_sw",
                budget=budget,
            )
            sw_result["attempt"] = attempt_index
            sw_attempts.append(sw_result)

    return {
        "benchmark": "matched_budget_efficacy",
        "provider": continuation_adapter,
        "budget": budget,
        "attempts": attempts,
        "token_unit": token_unit(),
        "host_contract": "get_context → assemble → call → post_turn",
        "substrate": "sqlite",
        "ncp": {
            "attempts": ncp_attempts,
            "summary": _summarize(ncp_attempts),
        },
        "sliding_window": {
            "attempts": sw_attempts,
            "summary": _summarize(sw_attempts),
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import json

    parser = argparse.ArgumentParser(
        description="Matched-budget efficacy benchmark — NCP vs sliding-window."
    )
    parser.add_argument(
        "--continuation-adapter",
        required=True,
        help="Adapter name: claude-cli, opencode-cli, codex-cli, local, anthropic, …",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=600,
        help="Token budget for context window (default: 600)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="Number of attempts per condition (default: 1)",
    )
    parser.add_argument(
        "--adapter-timeout-seconds",
        type=float,
        default=None,
        dest="adapter_timeout_seconds",
        help="Adapter call timeout in seconds (default: use adapter default)",
    )
    parser.add_argument(
        "--pipeline-id",
        default="bench_efficacy",
        dest="pipeline_id",
        help="Pipeline ID prefix for store records (default: bench_efficacy)",
    )
    args = parser.parse_args()

    artifact = run_efficacy(
        continuation_adapter=args.continuation_adapter,
        budget=args.budget,
        attempts=args.attempts,
        adapter_timeout_seconds=args.adapter_timeout_seconds,
        pipeline_id=args.pipeline_id,
    )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
