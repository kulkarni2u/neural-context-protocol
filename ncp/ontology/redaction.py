"""Attribute-key redaction for explicit ontology reads (design §8).

NCP has value-pattern secret scrubbing for handoff transcripts
(``ncp.agent_handoff._redact_secrets``) but no key-name helper, so ontology
reads use this one: any attribute whose *key* looks like a credential is
replaced by :data:`REDACTED`, recursively through nested objects and arrays.
Schema-pack ``sensitive`` fields are redacted as well. Admin callers see raw
values; automatic context never serializes attributes at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from pydantic import JsonValue

REDACTED = "[REDACTED]"

# Matched anywhere in the key, case-insensitively, after folding '-'/' ' to '_'.
SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|api_?key|private_?key|credential",
    re.IGNORECASE,
)


def is_sensitive_key(key: str, extra: Iterable[str] = ()) -> bool:
    folded = re.sub(r"[\s-]+", "_", key)
    if SENSITIVE_KEY_PATTERN.search(folded):
        return True
    return key in set(extra)


def redact_attributes(value: JsonValue, *, extra: Iterable[str] = ()) -> JsonValue:
    """A copy of ``value`` with sensitive keys' values replaced by :data:`REDACTED`."""
    names = frozenset(extra)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key, names) else redact_attributes(item, extra=names)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_attributes(item, extra=names) for item in value]
    return value


__all__ = ["REDACTED", "SENSITIVE_KEY_PATTERN", "is_sensitive_key", "redact_attributes"]
