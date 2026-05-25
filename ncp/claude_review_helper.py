"""Helpers for bounded Claude stream-json review runs."""

from __future__ import annotations

import json
from typing import Any


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from plain text or fenced markdown."""

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].lstrip()

    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("Claude review payload must be a JSON object")
    return parsed


def extract_text_event_payload(line: str) -> str | None:
    """Return text payload from one stream-json event line, if present."""

    raw = line.strip()
    if not raw:
        return None
    payload = json.loads(raw)
    if payload.get("type") != "text":
        return None
    part = payload.get("part", {})
    if not isinstance(part, dict):
        return None
    text = part.get("text")
    if not isinstance(text, str):
        return None
    return text


def extract_assistant_event_payload(line: str) -> str | None:
    """Return text payload from an assistant message event, if present."""

    raw = line.strip()
    if not raw:
        return None
    payload = json.loads(raw)
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message", {})
    if not isinstance(message, dict):
        return None
    content = message.get("content", [])
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        if item_type == "tool_use" and item.get("name") == "StructuredOutput":
            tool_input = item.get("input")
            if isinstance(tool_input, dict):
                parts.append(json.dumps(tool_input))
    if not parts:
        return None
    return "\n".join(parts)
