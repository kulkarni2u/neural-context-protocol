"""CAP-T5: computed drift signal.

Today ``ConsciousBlock.drift_score`` is entirely self-reported: an agent (or
a ``world_check`` whisper) simply asserts a number in ``[0.0, 1.0]`` and NCP
trusts it verbatim. Nothing stops a drifted agent from claiming ``0.0``. This
module replaces that honor system with a signal NCP computes itself from
observable turn history, while keeping the self-reported value visible for
comparison (that divergence is the trust story -- see
``docs/NCP_PROTOCOL_SPEC.md`` and ``ncp/mcp/server.py``'s ``get_context``
wiring).

Formula (normative)
---------------------------------------------------------------------------
The ALWAYS-available baseline is deterministic and lexical, so it needs no
optional dependency and never fails:

  1. Tokenize ``current_task_text`` into a lowercased term set (the same
     ``normalize_query_terms`` tokenization ``ncp/stores/retrieval.py`` uses
     for BM25 query terms -- ``text.lower().split()``).
  2. Take the last ``window_turns`` entries of ``recent_turns`` (a sliding
     window of prior turn summary strings, oldest-first; longer histories are
     simply clamped to the tail). Tokenize each the same way and union them
     into one "window token set".
  3. Score = ``1 - jaccard(task_tokens, window_tokens)`` where
     ``jaccard(a, b) = |a & b| / |a | b|``, clamped to ``[0.0, 1.0]``.
     0.0 means the current task's vocabulary is identical to the recent
     window (on-topic); 1.0 means no overlap at all (fully drifted).
  4. Edge cases that would make Jaccard undefined (no window turns, or an
     empty/blank task text) score ``0.0`` -- there is no observable evidence
     of divergence, so the safe default is "not drifted", not a crash.

This lexical score is the fallback in every configuration and is what a
disabled/unavailable embedding path always degrades to. ``DriftReading.method``
reports ``"lexical"`` whenever this is the whole story.

Optional embedding blend
---------------------------------------------------------------------------
When ``use_embeddings=True`` and a usable embedding adapter is available
(either passed explicitly -- the path unit tests use to mock it -- or the
local ``fastembed``-backed adapter from ``ncp/adapters/embedding.py`` when
importable), an embedding-cosine-distance term is blended in:

  embedding_distance = 1 - cosine_similarity(embed(task), centroid(embed(window)))
  score = 0.5 * embedding_distance + 0.5 * lexical_score

``method`` reports ``"blended"`` only when the embedding term was actually
used; any failure (adapter unavailable, import error, embed() raising) is
caught and the function silently falls back to the pure lexical score with
``method="lexical"`` -- fastembed is never a hard requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from ncp.stores.retrieval import normalize_query_terms

# Weight given to the embedding-distance term when blending (the remainder
# goes to the always-available lexical score). Fixed and documented, not
# tunable config, so a "blended" reading stays reproducible.
_EMBEDDING_BLEND_WEIGHT = 0.5

DriftMethod = str  # "lexical" | "blended"


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """Structural type for anything with fastembed/OpenAI-adapter shape."""

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class DriftReading:
    """Result of one ``compute_drift`` call."""

    score: float
    method: DriftMethod
    window_turns: int
    turns_considered: int

    def to_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 4),
            "method": self.method,
            "window_turns": self.window_turns,
        }


def compute_drift(
    recent_turns: Sequence[str],
    current_task_text: str,
    *,
    window_turns: int = 5,
    use_embeddings: bool = False,
    embedding_adapter: EmbeddingAdapter | None = None,
) -> DriftReading:
    """Compute a drift reading in ``[0.0, 1.0]`` from observable turn history.

    Parameters
    ----------
    recent_turns:
        Prior turn summary strings (e.g. ``f"{task} {slot} {result}"``),
        oldest-first. An empty sequence (a pipeline's first turn) is valid
        and always yields ``score=0.0``.
    current_task_text:
        The current turn's task/slot text to compare against the window.
        An empty/blank string always yields ``score=0.0`` (no signal to
        compare).
    window_turns:
        Sliding-window size. Clamped to ``>= 0``; only the most recent
        ``window_turns`` entries of ``recent_turns`` are ever considered, so
        arbitrarily long histories cost the same to score.
    use_embeddings:
        Opt-in embedding blend (see module docstring). Ignored (treated as
        the lexical-only path) whenever no usable adapter can be obtained.
    embedding_adapter:
        Pre-built adapter exposing ``.embed(text) -> list[float]``. Tests
        should pass a mock/fake here rather than relying on fastembed being
        installed. When ``None`` and ``use_embeddings`` is set, a local
        fastembed-backed adapter is constructed lazily; if fastembed is not
        importable this silently degrades to lexical-only.
    """

    window = max(0, int(window_turns))
    windowed_turns = list(recent_turns)[-window:] if window > 0 else []
    turns_considered = len(windowed_turns)

    lexical_score = _lexical_drift(current_task_text, windowed_turns)

    if not use_embeddings:
        return DriftReading(
            score=lexical_score,
            method="lexical",
            window_turns=window,
            turns_considered=turns_considered,
        )

    adapter = embedding_adapter if embedding_adapter is not None else _default_embedding_adapter()
    if adapter is None or turns_considered == 0 or not current_task_text.strip():
        return DriftReading(
            score=lexical_score,
            method="lexical",
            window_turns=window,
            turns_considered=turns_considered,
        )

    embedding_distance = _embedding_drift(adapter, current_task_text, windowed_turns)
    if embedding_distance is None:
        return DriftReading(
            score=lexical_score,
            method="lexical",
            window_turns=window,
            turns_considered=turns_considered,
        )

    blended = _EMBEDDING_BLEND_WEIGHT * embedding_distance + (1.0 - _EMBEDDING_BLEND_WEIGHT) * lexical_score
    return DriftReading(
        score=max(0.0, min(1.0, blended)),
        method="blended",
        window_turns=window,
        turns_considered=turns_considered,
    )


def _lexical_drift(current_task_text: str, windowed_turns: list[str]) -> float:
    if not windowed_turns:
        return 0.0
    task_tokens = normalize_query_terms(current_task_text)
    if not task_tokens:
        return 0.0
    window_tokens: set[str] = set()
    for turn_text in windowed_turns:
        window_tokens |= normalize_query_terms(turn_text)
    if not window_tokens:
        return 0.0
    union = task_tokens | window_tokens
    if not union:
        return 0.0
    jaccard = len(task_tokens & window_tokens) / len(union)
    return max(0.0, min(1.0, 1.0 - jaccard))


def _embedding_drift(
    adapter: EmbeddingAdapter,
    current_task_text: str,
    windowed_turns: list[str],
) -> float | None:
    try:
        task_vector = adapter.embed(current_task_text)
        turn_vectors = [adapter.embed(text) for text in windowed_turns if text.strip()]
    except Exception:
        return None
    if not task_vector or not turn_vectors:
        return None
    centroid = _centroid(turn_vectors)
    similarity = _cosine_similarity(task_vector, centroid)
    if similarity is None:
        return None
    return max(0.0, min(1.0, 1.0 - similarity))


def _centroid(vectors: list[list[float]]) -> list[float] | None:
    length = len(vectors[0])
    if length == 0 or any(len(vector) != length for vector in vectors):
        return None
    sums = [0.0] * length
    for vector in vectors:
        for index, value in enumerate(vector):
            sums[index] += value
    return [total / len(vectors) for total in sums]


def _cosine_similarity(a: list[float], b: list[float] | None) -> float | None:
    if b is None or len(a) != len(b) or not a:
        return None
    dot = sum(left * right for left, right in zip(a, b, strict=True))
    norm_a = sum(value * value for value in a) ** 0.5
    norm_b = sum(value * value for value in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def _default_embedding_adapter() -> EmbeddingAdapter | None:
    """Best-effort local embedding adapter; ``None`` when unavailable.

    fastembed is an optional dependency (``[local-embeddings]`` extra) and
    must never become a hard requirement of this module.
    """
    try:
        from ncp.adapters.embedding import LocalEmbeddingAdapter

        return LocalEmbeddingAdapter()
    except Exception:
        return None
