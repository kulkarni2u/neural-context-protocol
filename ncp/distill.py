"""Deterministic extractive distillation for oversized context chunks."""

from __future__ import annotations

import re

from ncp.stores.retrieval import normalize_query_terms
from ncp.tokens import estimate_tokens


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def _split_units(content: str) -> list[str]:
    units: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        units.extend(part.strip() for part in _SENTENCE_BOUNDARY_RE.split(stripped) if part.strip())
    return units or [content.strip()]


def distill_chunk(content: str, query_text: str, *, max_tokens: int) -> str:
    """Select query-relevant sentences/lines up to ``max_tokens``.

    The function is extractive only: it never rewrites text, and selected units
    are emitted in original order after scoring against normalized query terms.
    """

    if max_tokens <= 0:
        return ""
    query_terms = normalize_query_terms(query_text)
    units = _split_units(content)
    scored: list[tuple[int, int, int, str]] = []
    for index, unit in enumerate(units):
        unit_terms = normalize_query_terms(unit)
        overlap = len(query_terms.intersection(unit_terms))
        if query_terms and overlap == 0:
            continue
        scored.append((overlap, len(unit_terms), -index, unit))
    if not scored:
        scored = [(0, len(normalize_query_terms(unit)), -index, unit) for index, unit in enumerate(units)]

    selected: list[tuple[int, str]] = []
    used_tokens = 0
    for _, _, neg_index, unit in sorted(scored, reverse=True):
        cost = estimate_tokens(unit)
        if cost > max_tokens:
            continue
        if used_tokens + cost > max_tokens:
            continue
        selected.append((-neg_index, unit))
        used_tokens += cost
        if used_tokens >= max_tokens:
            break

    selected.sort(key=lambda item: item[0])
    return " ".join(unit for _, unit in selected)
