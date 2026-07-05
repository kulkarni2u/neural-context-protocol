"""Memoization helpers for CAP-C3 semantic work memoization."""

from __future__ import annotations

import hashlib
import re


def compute_memo_signature(task: str, context: str = "") -> str:
    """Produce a deterministic SHA-256 hex digest from normalized task + context.

    Normalizes by lowercasing, stripping, and collapsing whitespace.
    """
    normalized_task = re.sub(r"\s+", " ", task.strip().lower())
    normalized_context = re.sub(r"\s+", " ", context.strip().lower())
    raw = f"{normalized_task}||{normalized_context}" if normalized_context else normalized_task
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
