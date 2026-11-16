"""Registry of removed and deprecated NCP surface, plus a warn-once helper.

Policy (see CONTRIBUTING.md): a public config key or module is never removed
without at least one release that emits a ``DeprecationWarning`` (via
:func:`warn_deprecated`) and lists the entry in ``DEPRECATED``. When it is
finally removed the entry moves to ``REMOVED`` so ``ncp doctor`` keeps telling
users whose config still uses it, instead of NCP ignoring it silently.

Config entries use dotted paths relative to ``.ncp/config.toml``: ``"section"``
for a whole table or ``"section.key"`` for one key.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from ncp.config import DEFAULT_CONFIG


@dataclass(frozen=True)
class Deprecation:
    """One removed or deprecated config key / module."""

    name: str  # "section", "section.key" (kind="config") or "ncp.module" (kind="module")
    kind: str  # "config" or "module"
    since: str  # version that deprecated / removed it
    removal: str | None = None  # version that removes it (DEPRECATED only)
    replacement: str = ""  # what to use instead, or a short note


# Removed in a past release; the config/module is ignored (or gone) now.
# Only add entries that are really removed. ``ncp.skill_cache`` was removed in
# 1.9.2 and restored later, so it is intentionally not listed.
REMOVED: tuple[Deprecation, ...] = ()

# Still works, but scheduled for removal.
DEPRECATED: tuple[Deprecation, ...] = ()

# Top-level config tables NCP reads that are absent from DEFAULT_CONFIG.
_EXTRA_READ_SECTIONS = frozenset({"ontology", "context_policies", "skill_cache"})

# Tables whose keys are user-defined, so their contents are never validated.
_FREE_FORM_SECTIONS = frozenset({"principals", "decision_schemas", "providers"})


def _has_path(values: dict[str, Any], dotted: str) -> bool:
    node: Any = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def deprecated_config_usage(
    values: dict[str, Any],
    *,
    removed: tuple[Deprecation, ...] | None = None,
    deprecated: tuple[Deprecation, ...] | None = None,
) -> list[tuple[str, Deprecation]]:
    """Return ``("removed" | "deprecated", entry)`` for each registry config entry present in ``values``."""

    hits: list[tuple[str, Deprecation]] = []
    for label, entries in (
        ("removed", REMOVED if removed is None else removed),
        ("deprecated", DEPRECATED if deprecated is None else deprecated),
    ):
        for entry in entries:
            if entry.kind == "config" and _has_path(values, entry.name):
                hits.append((label, entry))
    return hits


def unknown_config_keys(values: dict[str, Any]) -> list[str]:
    """Return dotted names of tables / keys in ``values`` that NCP never reads."""

    unknown: list[str] = []
    for section, body in values.items():
        if section in _EXTRA_READ_SECTIONS:
            continue
        if section not in DEFAULT_CONFIG:
            unknown.append(section)
            continue
        known = DEFAULT_CONFIG[section]
        if section in _FREE_FORM_SECTIONS or not isinstance(known, dict) or not isinstance(body, dict):
            continue
        unknown.extend(f"{section}.{key}" for key in body if key not in known)
    return sorted(unknown)


_warned: set[str] = set()


def warn_deprecated(name: str, *, since: str, removal: str, replacement: str = "") -> None:
    """Emit a ``DeprecationWarning`` for ``name`` once per process.

    Use this in any public function/module/config path that is scheduled for
    removal, and add a matching ``DEPRECATED`` entry.
    """

    if name in _warned:
        return
    _warned.add(name)
    message = f"{name} is deprecated since {since} and will be removed in {removal}."
    if replacement:
        message += f" Use {replacement} instead."
    warnings.warn(message, DeprecationWarning, stacklevel=3)
