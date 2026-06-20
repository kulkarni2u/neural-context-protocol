"""Type-aware chunker and ingestion-time content filter for NCP.

The chunker splits content into bounded pieces for embedding and retrieval.
The filter applies deterministic noise-reduction (inspired by rtk/Headroom-style
payload compression) *before* chunking so that stored chunks contain signal,
not framing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal


ChunkTypeHint = Literal["auto", "prose", "json", "code", "table"]


# ---------------------------------------------------------------------------
# Ingestion-time content filtering
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROGRESS_RE = re.compile(r"^\s*\d{1,3}%\s*[|█▓▒░#\-=>\s]*", re.MULTILINE)
_TIMING_RE = re.compile(r"^\s*(real|user|sys)\s+\d+m[\d.]+s\s*$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class FilterResult:
    """Outcome of ingestion-time content filtering."""

    filtered: str
    raw_len: int
    filtered_len: int
    was_filtered: bool

    @property
    def reduction_ratio(self) -> float:
        if self.raw_len == 0:
            return 0.0
        return 1.0 - (self.filtered_len / self.raw_len)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI_RE.sub("", text)


def collapse_blank_lines(text: str) -> str:
    """Collapse runs of 3+ blank lines into a single blank line."""
    return _BLANK_RUN_RE.sub("\n\n", text)


def dedup_consecutive_lines(text: str) -> str:
    """Collapse consecutive duplicate lines with a count annotation."""
    lines = text.splitlines()
    if not lines:
        return text
    result: list[str] = []
    prev = lines[0]
    count = 1
    for line in lines[1:]:
        if line == prev:
            count += 1
        else:
            result.append(prev if count == 1 else f"{prev}  (×{count})")
            prev = line
            count = 1
    result.append(prev if count == 1 else f"{prev}  (×{count})")
    return "\n".join(result)


def strip_boilerplate(text: str) -> str:
    """Strip common tool-output boilerplate (progress bars, timing lines)."""
    text = _PROGRESS_RE.sub("", text)
    text = _TIMING_RE.sub("", text)
    return text


def filter_json_noise(text: str) -> str:
    """Prune null/empty values from top-level JSON to reduce noise."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict):
        pruned = {k: v for k, v in payload.items() if v is not None and v != "" and v != []}
        if pruned != payload:
            return json.dumps(pruned, ensure_ascii=True, separators=(",", ":"))
    return text


def filter_content(content: str, *, content_type: ChunkTypeHint = "auto") -> FilterResult:
    """Apply deterministic noise-reduction filters before chunking.

    Returns a FilterResult with the cleaned content and metadata about
    what changed. Filtering is always lossless in intent — no semantic
    information is removed, only framing noise.
    """
    raw_len = len(content)
    resolved = detect_type(content) if content_type == "auto" else content_type

    text = strip_ansi(content)
    text = collapse_blank_lines(text)
    text = dedup_consecutive_lines(text)

    if resolved in ("prose", "code"):
        text = strip_boilerplate(text)
    elif resolved == "json":
        text = filter_json_noise(text)

    text = text.strip()
    filtered_len = len(text)
    return FilterResult(
        filtered=text,
        raw_len=raw_len,
        filtered_len=filtered_len,
        was_filtered=(text != content.strip()),
    )


def _token_count(text: str) -> int:
    return len(text.split())


def _chunk_words(text: str, *, max_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    return [
        " ".join(words[index : index + max_tokens])
        for index in range(0, len(words), max_tokens)
    ]


def detect_type(content: str) -> ChunkTypeHint:
    """Detect the most likely chunking strategy for raw content."""

    stripped = content.lstrip()
    if not stripped:
        return "prose"

    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass

    if stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("```"):
        return "code"

    lines = [line for line in content.splitlines() if line.strip()]
    pipe_lines = [line for line in lines if "|" in line]
    if len(pipe_lines) >= 2:
        return "table"

    return "prose"


def chunk_content(content: str, *, chunk_type: ChunkTypeHint = "auto", max_tokens: int = 200) -> list[str]:
    """Dispatch content to the right strategy."""

    resolved_type = detect_type(content) if chunk_type == "auto" else chunk_type
    if resolved_type == "json":
        return chunk_json(content, max_tokens=max_tokens)
    if resolved_type == "code":
        return chunk_code(content, max_tokens=max_tokens)
    if resolved_type == "table":
        return chunk_table(content)
    return chunk_prose(content, max_tokens=max_tokens)


def chunk_prose(content: str, *, max_tokens: int = 200) -> list[str]:
    """Split prose at sentence boundaries, falling back to word windows."""

    stripped = content.strip()
    if not stripped:
        return []

    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", stripped) if segment.strip()]
    if not sentences:
        return _chunk_words(stripped, max_tokens=max_tokens)

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = _token_count(sentence)
        if sentence_tokens > max_tokens:
            if current:
                chunks.append(" ".join(current))
                current = []
                current_tokens = 0
            chunks.extend(_chunk_words(sentence, max_tokens=max_tokens))
            continue
        if current and current_tokens + sentence_tokens > max_tokens:
            chunks.append(" ".join(current))
            current = [sentence]
            current_tokens = sentence_tokens
            continue
        current.append(sentence)
        current_tokens += sentence_tokens

    if current:
        chunks.append(" ".join(current))

    return chunks


def chunk_json(content: str, *, max_tokens: int = 200) -> list[str]:
    """Split JSON by top-level keys, recursing up to two levels for large values."""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return chunk_prose(content, max_tokens=max_tokens)

    return _chunk_json_value(payload, max_tokens=max_tokens, depth=0)


_JSON_MAX_DEPTH = 2


def _chunk_json_value(value: Any, *, max_tokens: int, depth: int) -> list[str]:
    if isinstance(value, dict):
        chunks: list[str] = []
        for key, item in value.items():
            serialized = json.dumps({key: item}, ensure_ascii=True, separators=(",", ":"))
            if _token_count(serialized) <= max_tokens or depth >= _JSON_MAX_DEPTH:
                chunks.append(serialized)
                continue
            if isinstance(item, dict):
                for child_key, child_value in item.items():
                    child_serialized = json.dumps(
                        {key: {child_key: child_value}},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    if _token_count(child_serialized) <= max_tokens or depth + 1 >= _JSON_MAX_DEPTH:
                        chunks.append(child_serialized)
                    else:
                        chunks.extend(
                            _chunk_json_value({child_key: child_value}, max_tokens=max_tokens, depth=depth + 1)
                        )
                continue
            if isinstance(item, list):
                chunks.extend(_chunk_json_list(key, item, max_tokens=max_tokens, depth=depth))
                continue
            chunks.extend(_chunk_words(serialized, max_tokens=max_tokens))
        return chunks

    if isinstance(value, list):
        return _chunk_json_list("items", value, max_tokens=max_tokens, depth=depth)

    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return _chunk_words(serialized, max_tokens=max_tokens) or [serialized]


def _chunk_json_list(key: str, values: list[Any], *, max_tokens: int, depth: int = 0) -> list[str]:
    chunks: list[str] = []
    for index, item in enumerate(values):
        serialized = json.dumps({key: [{index: item}]}, ensure_ascii=True, separators=(",", ":"))
        if _token_count(serialized) <= max_tokens or depth >= _JSON_MAX_DEPTH:
            chunks.append(serialized)
        else:
            chunks.extend(_chunk_words(serialized, max_tokens=max_tokens))
    return chunks


def chunk_code(content: str, *, max_tokens: int = 200, fallback_lines: int = 30) -> list[str]:
    """Split code by function/class boundaries, falling back to 30-line windows."""

    stripped = content.strip()
    if not stripped:
        return []

    if stripped.startswith("```") and stripped.endswith("```"):
        inner_lines = stripped.splitlines()[1:-1]
    else:
        inner_lines = content.splitlines()

    boundaries = [
        index
        for index, line in enumerate(inner_lines)
        if line.startswith("def ") or line.startswith("class ")
    ]

    if not boundaries:
        return _chunk_code_by_lines(inner_lines, fallback_lines=fallback_lines)

    starts = [0] + boundaries if boundaries[0] != 0 else boundaries
    chunks: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(inner_lines)
        segment = "\n".join(inner_lines[start:end]).strip()
        if not segment:
            continue
        if _token_count(segment) <= max_tokens:
            chunks.append(segment)
        else:
            chunks.extend(_split_oversized_code_segment(segment, max_tokens=max_tokens, fallback_lines=fallback_lines))
    return chunks


def _split_oversized_code_segment(segment: str, *, max_tokens: int, fallback_lines: int) -> list[str]:
    """Split an oversized code segment at method boundaries before falling back to line windows.

    When a class body is too large to fit in one chunk, this tries splitting at
    indented def/class lines (one indentation level) before using fixed line windows,
    which would otherwise break methods mid-statement.
    """
    lines = segment.splitlines()
    method_boundaries = [
        i for i, line in enumerate(lines)
        if i > 0 and re.match(r"^[ \t]+(def |class )", line)
    ]
    if method_boundaries:
        starts = [0] + method_boundaries
        sub_chunks: list[str] = []
        for pos, start in enumerate(starts):
            end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
            sub = "\n".join(lines[start:end]).strip()
            if not sub:
                continue
            if _token_count(sub) <= max_tokens:
                sub_chunks.append(sub)
            else:
                sub_chunks.extend(_chunk_code_by_lines(sub.splitlines(), fallback_lines=fallback_lines))
        return sub_chunks
    return _chunk_code_by_lines(lines, fallback_lines=fallback_lines)


def _chunk_code_by_lines(lines: Iterable[str], *, fallback_lines: int) -> list[str]:
    line_list = list(lines)
    chunks: list[str] = []
    for index in range(0, len(line_list), fallback_lines):
        segment = "\n".join(line_list[index : index + fallback_lines]).strip()
        if segment:
            chunks.append(segment)
    return chunks


def chunk_table(content: str) -> list[str]:
    """Split markdown-like tables into chunks of five data rows with the header preserved."""

    lines = [line.rstrip() for line in content.splitlines() if line.strip()]
    if len(lines) <= 2:
        return [content.strip()] if content.strip() else []

    header = lines[0]
    separator = lines[1] if set(lines[1].replace("|", "").replace("-", "").replace(":", "").strip()) == set() else None
    data_rows = lines[2:] if separator else lines[1:]

    chunks: list[str] = []
    for index in range(0, len(data_rows), 5):
        rows = data_rows[index : index + 5]
        block_lines = [header]
        if separator is not None:
            block_lines.append(separator)
        block_lines.extend(rows)
        chunks.append("\n".join(block_lines))
    return chunks
