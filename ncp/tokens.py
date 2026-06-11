"""Token-counting helpers shared by assembly and benchmarks.

Counting is deterministic by default (chars/4) so that budget enforcement and
benchmark verdicts do not depend on whether tiktoken's encoding file could be
downloaded in the current environment. Set ``NCP_TOKEN_UNIT=tiktoken`` to opt
in to cl100k_base counting when tiktoken and its encoding data are available.
"""

from __future__ import annotations

import os
from typing import cast

_UNSET = object()
_encoder: object = _UNSET


def _load_tiktoken_encoder() -> object | None:
    if os.environ.get("NCP_TOKEN_UNIT", "chars_div4") != "tiktoken":
        return None
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _resolve_encoder() -> object | None:
    global _encoder
    if _encoder is _UNSET:
        _encoder = _load_tiktoken_encoder()
    return None if _encoder is _UNSET else cast("object | None", _encoder)


def reset_encoder_cache() -> None:
    """Re-read NCP_TOKEN_UNIT on next call. Intended for tests."""

    global _encoder
    _encoder = _UNSET


def estimate_tokens(text: str) -> int:
    """Estimate token count using the configured unit.

    Defaults to the standard 4-chars-per-token heuristic, which is
    deterministic across environments. With ``NCP_TOKEN_UNIT=tiktoken`` and a
    loadable cl100k_base encoding, counts real BPE tokens instead.
    """

    stripped = text.strip()
    if not stripped:
        return 0
    encoder = _resolve_encoder()
    if encoder is not None:
        return len(encoder.encode(stripped))  # type: ignore[attr-defined]
    return max(1, len(stripped) // 4)


def token_unit() -> str:
    """Return the token-counting unit used by estimate_tokens()."""

    return "tiktoken/cl100k_base" if _resolve_encoder() is not None else "chars_div4"
