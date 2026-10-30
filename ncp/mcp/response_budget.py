"""Bound the serialized size of an ``ncp_get_context`` response.

The assembled ``context`` honors ``max_tokens``, but the tool response a model
actually reads also carries telemetry, tier hints, drift and budget blocks.
``fit_response`` caps the whole serialized response: optional diagnostics go
first, then the context is re-assembled under a smaller budget. Evidence a
later ``ncp_post_turn``/``ncp_record_outcome`` needs (``retrieved_chunk_ids``,
``pending_whisper_ids``, ``session_id``, the policy identity) is never
dropped. Sizes use ``ncp.tokens.estimate_tokens`` over the JSON text, the same
estimator the assembler budgets with.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from ncp.tokens import estimate_tokens

# Steps applied in order until the response fits. Each removes diagnostics
# only: (label, keys to delete from the response, telemetry keys to empty).
_TRIM_STEPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("telemetry_lists", (), ("evicted_high_relevance", "evicted_whispers", "fanin_contradictions")),
    ("tier_factors", ("factors",), ()),
    ("telemetry", ("telemetry",), ()),
    ("drift", ("drift",), ()),
    ("budget_tokens", ("budget_tokens",), ()),
    ("budget", ("budget",), ()),
    ("tier_hint", ("tier_hint", "complexity_signal"), ()),
)
_MAX_REASSEMBLIES = 4

# A callback that re-assembles the context under a token ceiling and returns
# the fields that depend on it: context, pending_whisper_ids, retrieved_chunk_ids.
Reassemble = Callable[[int], dict[str, Any]]


def response_tokens(response: dict[str, Any]) -> int:
    return estimate_tokens(json.dumps(response))


def compact_telemetry(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Counts and flags only, with zero/false/empty values omitted."""
    compact: dict[str, Any] = {}
    for key in ("evicted_chunk_count", "evicted_whisper_count", "fetch_budget_remaining", "fanin_merged_count"):
        if telemetry.get(key):
            compact[key] = telemetry[key]
    if telemetry.get("retrieval_used_fallback"):
        compact["retrieval_used_fallback"] = True
    if telemetry.get("fetch_hint"):
        compact["fetch_hint"] = telemetry["fetch_hint"]
    contradictions = telemetry.get("fanin_contradictions") or []
    if contradictions:
        # Contradicting evidence ids stay visible: the reader must reason about them.
        compact["fanin_contradictions"] = contradictions
    return compact


def context_sha256(context: str) -> str:
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def fit_response(
    response: dict[str, Any],
    max_tokens: int,
    *,
    reassemble: Reassemble | None = None,
) -> dict[str, Any]:
    """Return ``response`` trimmed to at most ``max_tokens`` serialized tokens.

    An already-fitting response is returned unchanged (no ``truncated`` key).
    Otherwise a ``truncated`` block lists what was dropped, the context
    ceiling used if the context was re-assembled, and ``fits`` -- False only
    when the required fields alone exceed ``max_tokens``.
    """
    if max_tokens < 1:
        raise ValueError("response_max_tokens must be >= 1")
    if response_tokens(response) <= max_tokens:
        return response

    fitted = dict(response)
    if isinstance(fitted.get("telemetry"), dict):
        fitted["telemetry"] = dict(fitted["telemetry"])
    truncated: dict[str, Any] = {"dropped": [], "fits": False}
    fitted["truncated"] = truncated  # measured too: its own size is reserved

    for label, keys, telemetry_keys in _TRIM_STEPS:
        if response_tokens(fitted) <= max_tokens:
            break
        changed = False
        for key in keys:
            if key in fitted:
                del fitted[key]
                changed = True
        telemetry = fitted.get("telemetry")
        if isinstance(telemetry, dict):
            for key in telemetry_keys:
                if telemetry.get(key):
                    telemetry[key] = []
                    changed = True
        if changed:
            truncated["dropped"].append(label)

    if response_tokens(fitted) > max_tokens and reassemble is not None:
        for _ in range(_MAX_REASSEMBLIES):
            overflow = response_tokens(fitted) - max_tokens
            if overflow <= 0:
                break
            ceiling = estimate_tokens(str(fitted.get("context", ""))) - overflow
            if ceiling < 1:
                break
            fitted.update(reassemble(ceiling))
            truncated["context_max_tokens"] = ceiling
            if "context_sha256" in fitted:
                fitted["context_sha256"] = context_sha256(str(fitted["context"]))

    truncated["fits"] = response_tokens(fitted) <= max_tokens
    return fitted
