"""Memoization helpers for CAP-C3 semantic work memoization."""

from __future__ import annotations

import ast
import hashlib
import re

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
