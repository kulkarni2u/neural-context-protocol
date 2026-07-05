"""CAP-T5: computed drift unit tests.

fastembed is an optional dependency and is intentionally NOT installed in
this test environment (see ncp/adapters/embedding.py). Embedding-blend
coverage below always injects a fake adapter rather than relying on
fastembed being importable.
"""

from __future__ import annotations

import pytest

from ncp.drift import DriftReading, compute_drift


class FakeEmbeddingAdapter:
    """Deterministic stand-in for LocalEmbeddingAdapter used in tests."""

    def __init__(self, vectors: dict[str, list[float]] | None = None, *, raises: bool = False) -> None:
        self._vectors = vectors or {}
        self._raises = raises
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self._raises:
            raise RuntimeError("boom")
        return self._vectors.get(text, [1.0, 0.0])


class EmptyVectorAdapter:
    def embed(self, text: str) -> list[float]:
        return []


# ---------------------------------------------------------------------------
# Lexical formula: known token sets -> known scores
# ---------------------------------------------------------------------------


def test_identical_task_and_window_yields_zero_drift() -> None:
    reading = compute_drift(["rebuild auth pipeline"], "rebuild auth pipeline")
    assert reading.score == pytest.approx(0.0)
    assert reading.method == "lexical"


def test_disjoint_task_and_window_yields_full_drift() -> None:
    reading = compute_drift(["rebuild auth pipeline"], "bake a chocolate cake")
    assert reading.score == pytest.approx(1.0)


def test_partial_overlap_matches_hand_computed_jaccard() -> None:
    # task tokens: {a, b, c, d}; window tokens: {b, c, x, y}
    # intersection = {b, c} = 2; union = {a, b, c, d, x, y} = 6 -> jaccard = 1/3
    reading = compute_drift(["b c x y"], "a b c d")
    assert reading.score == pytest.approx(1.0 - (2 / 6))


def test_tokenization_is_lowercase_whitespace_split() -> None:
    # Matches ncp.stores.retrieval.normalize_query_terms exactly.
    reading = compute_drift(["Rebuild AUTH Pipeline"], "rebuild auth pipeline")
    assert reading.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_prior_turns_scores_zero_and_does_not_crash() -> None:
    reading = compute_drift([], "brand new task")
    assert reading == DriftReading(score=0.0, method="lexical", window_turns=5, turns_considered=0)


def test_single_turn_history() -> None:
    reading = compute_drift(["only prior turn"], "only prior turn")
    assert reading.score == pytest.approx(0.0)
    assert reading.turns_considered == 1


def test_empty_task_text_scores_zero() -> None:
    reading = compute_drift(["some prior turn about billing"], "")
    assert reading.score == 0.0


def test_blank_task_text_scores_zero() -> None:
    reading = compute_drift(["some prior turn about billing"], "   ")
    assert reading.score == 0.0


def test_window_turns_with_only_blank_entries_scores_zero() -> None:
    reading = compute_drift(["", "   "], "anything")
    assert reading.score == 0.0


# ---------------------------------------------------------------------------
# Window behavior / clamping
# ---------------------------------------------------------------------------


def test_window_clamps_to_most_recent_n_turns() -> None:
    # Only the last 2 turns should matter; the old, unrelated turn falls out
    # of the window and must not affect the score.
    history = ["completely unrelated gardening talk", "auth", "pipeline"]
    reading = compute_drift(history, "auth pipeline", window_turns=2)
    assert reading.score == pytest.approx(0.0)
    assert reading.turns_considered == 2


def test_very_long_history_is_clamped_to_window_size() -> None:
    history = [f"topic_{i}" for i in range(500)]
    reading = compute_drift(history, "topic_499", window_turns=5)
    assert reading.turns_considered == 5
    # topic_499 is within the last 5 entries (495..499) so overlap exists.
    assert reading.score < 1.0


def test_window_turns_non_positive_uses_no_window() -> None:
    reading = compute_drift(["a b c"], "a b c", window_turns=0)
    assert reading.turns_considered == 0
    assert reading.score == 0.0


def test_window_turns_reported_reflects_configured_value() -> None:
    reading = compute_drift(["a"], "a", window_turns=7)
    assert reading.window_turns == 7


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_compute_drift_is_deterministic() -> None:
    history = ["fix the login bug", "investigate auth token expiry"]
    first = compute_drift(history, "auth token refresh flow")
    second = compute_drift(history, "auth token refresh flow")
    assert first == second


def test_score_is_always_within_unit_interval() -> None:
    histories = [[], ["x"], ["a b c", "d e f", "g h i"], [f"turn {i}" for i in range(50)]]
    tasks = ["", "   ", "a", "a b c d e", "z" * 500]
    for history in histories:
        for task in tasks:
            reading = compute_drift(history, task, window_turns=5)
            assert 0.0 <= reading.score <= 1.0


# ---------------------------------------------------------------------------
# Embedding blend (mocked adapter only -- fastembed is not a hard dependency)
# ---------------------------------------------------------------------------


def test_disabled_use_embeddings_never_touches_adapter() -> None:
    adapter = FakeEmbeddingAdapter()
    reading = compute_drift(["a b"], "a b", use_embeddings=False, embedding_adapter=adapter)
    assert reading.method == "lexical"
    assert adapter.calls == []


def test_blended_method_reported_when_adapter_available() -> None:
    adapter = FakeEmbeddingAdapter({"task": [1.0, 0.0], "history": [0.0, 1.0]})
    reading = compute_drift(["history"], "task", use_embeddings=True, embedding_adapter=adapter)
    assert reading.method == "blended"
    # Orthogonal vectors -> cosine similarity 0 -> embedding_distance 1.0;
    # lexical score is also 1.0 (no token overlap) -> blended stays 1.0.
    assert reading.score == pytest.approx(1.0)


def test_blend_combines_lexical_and_embedding_signal() -> None:
    # Identical vectors (embedding says "no drift") but disjoint tokens
    # (lexical says "full drift") should land exactly between the two.
    adapter = FakeEmbeddingAdapter({"task": [1.0, 0.0], "history": [1.0, 0.0]})
    reading = compute_drift(["history"], "task", use_embeddings=True, embedding_adapter=adapter)
    assert reading.method == "blended"
    assert reading.score == pytest.approx(0.5)


def test_embedding_adapter_exception_falls_back_to_lexical() -> None:
    adapter = FakeEmbeddingAdapter(raises=True)
    reading = compute_drift(["a b"], "a b", use_embeddings=True, embedding_adapter=adapter)
    assert reading.method == "lexical"
    assert reading.score == pytest.approx(0.0)


def test_empty_embedding_vector_falls_back_to_lexical() -> None:
    reading = compute_drift(["a b"], "a b", use_embeddings=True, embedding_adapter=EmptyVectorAdapter())
    assert reading.method == "lexical"


def test_use_embeddings_without_adapter_and_without_fastembed_falls_back() -> None:
    # fastembed is not installed in this environment; the module's lazy
    # default-adapter construction must fail closed to lexical, not raise.
    reading = compute_drift(["a b"], "a b", use_embeddings=True, embedding_adapter=None)
    assert reading.method == "lexical"


def test_use_embeddings_with_no_history_stays_lexical_even_with_adapter() -> None:
    adapter = FakeEmbeddingAdapter()
    reading = compute_drift([], "a task", use_embeddings=True, embedding_adapter=adapter)
    assert reading.method == "lexical"
    assert adapter.calls == []
