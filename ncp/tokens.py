"""Token-counting helpers shared by assembly and benchmarks."""

from __future__ import annotations

from typing import cast


def _load_tiktoken_encoder() -> object | None:
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


_TIKTOKEN_ENCODER = _load_tiktoken_encoder()


def estimate_tokens(text: str) -> int:
    """Estimate token count with a real tokenizer when available.

    Falls back to the standard 4-chars-per-token heuristic when tiktoken is
    unavailable, keeping offline benchmark and assembly behavior deterministic
    enough to compare across sandboxed environments.
    """

    stripped = text.strip()
    if not stripped:
        return 0
    if _TIKTOKEN_ENCODER is not None:
        return len(cast(object, _TIKTOKEN_ENCODER).encode(stripped))  # type: ignore[attr-defined]
    return max(1, len(stripped) // 4)


def token_unit() -> str:
    """Return the token-counting unit used by estimate_tokens()."""

    return "tiktoken/cl100k_base" if _TIKTOKEN_ENCODER is not None else "chars_div4"
