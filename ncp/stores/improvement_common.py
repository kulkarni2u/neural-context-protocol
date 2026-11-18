"""Backend-neutral helpers for improvement records (v1.9.7)."""

from __future__ import annotations

import hashlib
import time
from uuid import uuid4

# Fields ``update_improvement_attempt`` may change. Identity (attempt_id,
# problem_id re-pointing is done only by problem merge), kind and created_at
# are immutable.
ATTEMPT_UPDATABLE: frozenset[str] = frozenset(
    {
        "status", "accepted", "what", "rationale", "files", "candidate_chunk_id",
        "predicted", "fixed", "missed", "passed", "total", "mean_output_tokens",
        "anchor_rate", "anchor_output_tokens", "model", "benchmark", "note",
        "resolved_at",
    }
)
ATTEMPT_LIST_FIELDS: frozenset[str] = frozenset({"files", "predicted", "fixed", "missed"})
ATTEMPT_STATUSES: frozenset[str] = frozenset({"proposed", "applied", "rejected", "rolled_back"})


def normalize_problem_name(name: str) -> str:
    """Strip, collapse whitespace, casefold."""
    return " ".join(name.split()).casefold()


def make_problem_id(*, pipeline_id: str | None, scope: str, name: str) -> str:
    """Deterministic id: the same normalized name in the same (pipeline, scope) is one problem."""
    key = f"{pipeline_id or ''}|{scope}|{normalize_problem_name(name)}"
    return "prob_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def new_attempt_id() -> str:
    return f"att_{int(time.time() * 1000)}_{uuid4().hex[:12]}"


def validate_attempt_fields(fields: dict[str, object]) -> None:
    bad = sorted(set(fields) - ATTEMPT_UPDATABLE)
    if bad:
        raise ValueError(f"cannot update improvement attempt fields: {bad}")
    status = fields.get("status")
    if status is not None and status not in ATTEMPT_STATUSES:
        raise ValueError(f"invalid attempt status {status!r}")
