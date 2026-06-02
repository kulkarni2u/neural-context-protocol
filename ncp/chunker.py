"""Type-aware chunker for prose, JSON, code, and table content."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal


ChunkTypeHint = Literal["auto", "prose", "json", "code", "table"]


def _token_count(text: str) -> int:
    return len(text.split())


def _chunk_words(text: str, *, max_tokens: int) -> list[str]:
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be > 0, got {max_tokens}")
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
    """Split JSON by top-level keys, recursing one level for large values."""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return chunk_prose(content, max_tokens=max_tokens)

    return _chunk_json_value(payload, max_tokens=max_tokens, depth=0)


def _chunk_json_value(value: Any, *, max_tokens: int, depth: int) -> list[str]:
    if isinstance(value, dict):
        chunks: list[str] = []
        for key, item in value.items():
            serialized = json.dumps({key: item}, ensure_ascii=False, separators=(",", ":"))
            if _token_count(serialized) <= max_tokens or depth >= 1:
                chunks.append(serialized)
                continue
            if isinstance(item, dict):
                for child_key, child_value in item.items():
                    child_serialized = json.dumps(
                        {key: {child_key: child_value}},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if _token_count(child_serialized) <= max_tokens:
                        chunks.append(child_serialized)
                    else:
                        chunks.extend(_chunk_words(child_serialized, max_tokens=max_tokens))
                continue
            if isinstance(item, list):
                chunks.extend(_chunk_json_list(key, item, max_tokens=max_tokens))
                continue
            chunks.extend(_chunk_words(serialized, max_tokens=max_tokens))
        return chunks

    if isinstance(value, list):
        return _chunk_json_list("items", value, max_tokens=max_tokens)

    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _chunk_words(serialized, max_tokens=max_tokens) or [serialized]


def _chunk_json_list(key: str, values: list[Any], *, max_tokens: int) -> list[str]:
    chunks: list[str] = []
    for item in values:
        serialized = json.dumps({key: [item]}, ensure_ascii=False, separators=(",", ":"))
        if _token_count(serialized) <= max_tokens:
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
            chunks.extend(_chunk_code_by_lines(segment.splitlines(), fallback_lines=fallback_lines))
    return chunks


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
