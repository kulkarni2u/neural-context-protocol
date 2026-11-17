"""Token-counting helpers shared by assembly and benchmarks.

Counting is deterministic by default (chars/4) so that budget enforcement and
benchmark verdicts do not depend on whether tiktoken's encoding file could be
downloaded in the current environment. Set ``NCP_TOKEN_UNIT=tiktoken`` to opt
in to cl100k_base counting when tiktoken and its encoding data are available.
"""

from __future__ import annotations

import math
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


# Budget-enforcement counting (assembly only). ``estimate_tokens`` stays the raw,
# margin-free estimate that benchmarks and reports use; the assembler counts
# with ``estimate_budget_tokens`` so a block that "fits" is unlikely to overflow
# a real tokenizer.
DEFAULT_ESTIMATE_MARGIN = 0.9
_DENSE_CHARS_PER_TOKEN = 2.5
_DENSE_SYMBOL_FRACTION = 0.2


def _is_symbol_dense(text: str) -> bool:
    """True for mostly-code/JSON text, which tokenizes at far fewer chars/token."""

    if not text:
        return False
    symbols = sum(1 for ch in text if not (ch.isalnum() or ch.isspace()))
    return symbols / len(text) >= _DENSE_SYMBOL_FRACTION


def estimate_budget_tokens(text: str, *, margin: float = DEFAULT_ESTIMATE_MARGIN) -> int:
    """Conservative token estimate for budget enforcement.

    With the ``chars_div4`` unit the plain chars/4 estimate is divided by
    ``margin`` (0 < margin <= 1; 0.9 reserves 10%), and symbol-dense text
    (code, JSON) uses a denser chars-per-token ratio. A real tokenizer
    (``NCP_TOKEN_UNIT=tiktoken``) is already exact, so no margin is applied.
    """

    stripped = text.strip()
    if not stripped:
        return 0
    if _resolve_encoder() is not None:
        return estimate_tokens(stripped)
    margin = min(1.0, max(0.1, margin))
    # Non-ASCII text (CJK etc.) tokenizes near 1 token/char, so count it at 1.
    wide = sum(1 for ch in stripped if ord(ch) > 127)
    rest = stripped if not wide else "".join(ch for ch in stripped if ord(ch) <= 127)
    per_token = _DENSE_CHARS_PER_TOKEN if _is_symbol_dense(rest) else 4.0
    ascii_tokens = math.ceil(len(rest) / (per_token * margin)) if rest else 0
    return max(1, ascii_tokens + wide)
