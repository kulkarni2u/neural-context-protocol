"""Memoization helpers for CAP-C3 semantic work memoization."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable, Mapping

# Kinds for which `validate_code_memo_ast` checks for a specific top-level
# node type, beyond mere parseability.
_STRUCTURAL_KINDS = {"function", "class"}


def compute_memo_signature(task: str, context: str = "") -> str:
    """Produce a deterministic SHA-256 hex digest from normalized task + context.

    Normalizes by lowercasing, stripping, and collapsing whitespace.
    """
    normalized_task = re.sub(r"\s+", " ", task.strip().lower())
    normalized_context = re.sub(r"\s+", " ", context.strip().lower())
    raw = f"{normalized_task}||{normalized_context}" if normalized_context else normalized_task
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Bump when the canonical turn-signature payload changes, so memos keyed by
# an older layout can never collide with (and be served for) a new one.
TURN_MEMO_SIGNATURE_VERSION = "ncp-turn-memo/2"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def compute_dependency_fingerprint(
    *,
    evidence: Iterable[tuple[str, str]],
    whisper_ids: Iterable[str] = (),
    policy_sha256: str | None = None,
) -> str:
    """SHA-256 over everything a memoized result depended on besides the turn.

    ``evidence`` is ``(chunk_id, content)`` for each stored chunk in the
    model-visible context; content is hashed so an edited chunk changes the
    fingerprint even under the same id. Pending whisper ids and the applied
    context-policy hash are included because both change what the model
    saw. Order-insensitive.
    """
    payload = {
        "evidence": sorted(
            (str(cid), hashlib.sha256(content.encode("utf-8")).hexdigest()) for cid, content in evidence
        ),
        "whispers": sorted({str(w) for w in whisper_ids}),
        "policy": policy_sha256,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def compute_turn_memo_signature(
    *,
    turn: str,
    agent_id: str,
    role: str,
    pipeline_id: str | None,
    task: str,
    slot: str,
    intent: str,
    goal_version: int,
    owns: Iterable[str],
    must_not: Iterable[str],
    model: str,
    provider: str,
    dependency_fingerprint: str,
) -> str:
    """Canonical, versioned memo key for one ``ncp.run``/``ncp.stream`` turn.

    Two turns share a key only when the same agent, in the same pipeline,
    under the same goal and constraints, asks the same model/provider the
    same question over the same dependencies.
    """
    payload = {
        "v": TURN_MEMO_SIGNATURE_VERSION,
        "turn": _norm(turn),
        "agent_id": agent_id,
        "role": role,
        "pipeline_id": pipeline_id,
        "task": task,
        "slot": slot,
        "intent": intent,
        "goal_version": goal_version,
        "owns": sorted(owns),
        "must_not": sorted(must_not),
        "model": model,
        "provider": provider,
        "deps": dependency_fingerprint,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def memo_ineligibility(
    memo: Mapping[str, object],
    *,
    min_outcome: float,
    allow_unverified: bool,
    require_result: bool = True,
) -> str | None:
    """Why a found, fresh memo may not be reused -- or None when it may.

    The single reuse gate for every memo read path (``ncp_lookup_memo`` and
    ``ncp.run``/``ncp.stream``): an empty result, an outcome below
    ``min_outcome``, or (unless ``allow_unverified``) a result nobody has
    verified is never served. ``require_result=False`` admits memos that
    record only evidence ids (``ncp_record_memo`` without a summary).
    """
    if require_result and not memo.get("result_summary"):
        return "empty_result"
    try:
        outcome = float(memo.get("outcome", 0.0) or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        outcome = 0.0
    if outcome < min_outcome:
        return "outcome_below_min"
    if not allow_unverified and not bool(memo.get("verified", 0)):
        return "unverified"
    return None


def validate_code_memo_ast(content: str, kind: str) -> bool:
    """Structural sanity check for a code memo's content against its declared ``kind``.

    What this guarantees:
      * ``content`` is syntactically valid Python (``ast.parse`` succeeds) and
        has at least one top-level statement.
      * For ``kind == "function"``: at least one top-level ``def``/``async def``
        is present in the module body.
      * For ``kind == "class"``: at least one top-level ``class`` is present.
      * For any other ``kind`` (e.g. ``"module"``, or an unrecognized value):
        only the parse + non-empty check above applies (presence-only).

    What this does NOT guarantee: that the code is semantically correct, that
    it implements what the memo's task/summary claims, that it is safe to
    execute, or that any particular name/signature/behavior matches. This is
    a cheap structural presence check, not a correctness or safety proof —
    callers must not treat a ``True`` result as verification of behavior.

    Returns False if ``content`` is falsy or fails to parse as Python.
    """
    if not content:
        return False
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return False
    if not tree.body:
        return False
    if kind == "function":
        return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body)
    if kind == "class":
        return any(isinstance(node, ast.ClassDef) for node in tree.body)
    # Presence-only for "module" and any other declared kind.
    return True
