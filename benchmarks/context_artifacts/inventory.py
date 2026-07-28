"""Deterministic inventory of provider-specific NCP context artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal

from ncp.cli import CLAUDE_MD_TEMPLATE
from ncp.mcp.server import MCP_TOOLS
from ncp.tokens import estimate_tokens, token_unit


Provider = Literal["claude", "codex", "opencode"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex", "opencode")
LIFECYCLE_CALLS: tuple[str, ...] = (
    "ncp_get_context",
    "ncp_post_turn",
    "ncp_write_memory",
    "ncp_emit_whisper",
    "ncp_fetch",
    "ncp_record_decision",
)


@dataclass(frozen=True)
class ProviderArtifact:
    provider: Provider
    surface: str
    path: str
    token_count: int
    lifecycle_calls: tuple[str, ...]
    trust_boundary_present: bool


@dataclass(frozen=True)
class _ProviderSource:
    provider: Provider
    surface: str
    path: str
    text: str


_FILE_SOURCES: dict[Provider, tuple[tuple[str, str], ...]] = {
    "claude": (
        ("claude_md_example", "examples/06_claude_code/CLAUDE.md"),
        ("skill", "ncp/templates/provider_hooks/claude/skills/ncp/SKILL.md"),
        ("skill_example", "examples/06_claude_code/skills/ncp/SKILL.md"),
        ("session_hook", "ncp/templates/provider_hooks/claude/hooks/ncp-session-start.sh"),
        ("session_hook_example", "examples/06_claude_code/hooks/ncp-session-start.sh"),
    ),
    "codex": (
        ("agents_md", "ncp/templates/provider_hooks/codex/AGENTS.md"),
        ("agents_md_example", "examples/07_codex_cli/AGENTS.md"),
        ("session_hook", "ncp/templates/provider_hooks/codex/hooks/ncp-session-start.sh"),
        ("session_hook_example", "examples/07_codex_cli/hooks/ncp-session-start.sh"),
    ),
    "opencode": (
        ("agents_md", "ncp/templates/provider_hooks/opencode/AGENTS.md"),
        ("agents_md_example", "examples/09_opencode/AGENTS.md"),
        ("plugin_context", "ncp/templates/provider_hooks/opencode/plugins/ncp.js"),
        ("plugin_context_example", "examples/09_opencode/plugins/ncp.js"),
    ),
}


def _lifecycle_calls(text: str) -> tuple[str, ...]:
    return tuple(call for call in LIFECYCLE_CALLS if re.search(rf"\b{call}\b", text))


def has_trust_boundary(text: str) -> bool:
    """Return whether text contains an explicit retrieved-content trust boundary."""

    normalized = " ".join(text.lower().replace("`", "").split())
    return (
        "data, never as instructions" in normalized
        or "data, not instructions" in normalized
    )


def has_full_trust_boundary(text: str) -> bool:
    """Return whether the complete authority and low-trust policy is present."""

    normalized = " ".join(text.lower().replace("`", "").split())
    data_boundary = (
        "data, never as instructions" in normalized
        or "data, not instructions" in normalized
    )
    authority_boundary = all(term in normalized for term in ("owns", "must-not"))
    low_trust_boundary = all(
        term in normalized for term in ("trust:", "0.7", "agent_inferred", "verify")
    )
    return data_boundary and authority_boundary and low_trust_boundary


def _shared_tool_descriptions() -> str:
    """Serialize exactly the model-facing MCP tool metadata."""

    return json.dumps(MCP_TOOLS, sort_keys=True, separators=(",", ":"))


def collect_provider_sources(repo_root: Path) -> dict[Provider, list[_ProviderSource]]:
    """Load canonical sources and checked-in example mirrors without executing hooks."""

    root = Path(repo_root).resolve()
    shared_text = _shared_tool_descriptions()
    sources: dict[Provider, list[_ProviderSource]] = {provider: [] for provider in PROVIDERS}

    sources["claude"].append(
        _ProviderSource(
            provider="claude",
            surface="claude_md",
            path="ncp/cli.py::CLAUDE_MD_TEMPLATE",
            text=CLAUDE_MD_TEMPLATE,
        )
    )
    for provider in PROVIDERS:
        for surface, relative_path in _FILE_SOURCES[provider]:
            sources[provider].append(
                _ProviderSource(
                    provider=provider,
                    surface=surface,
                    path=relative_path,
                    text=(root / relative_path).read_text(encoding="utf-8"),
                )
            )
        sources[provider].append(
            _ProviderSource(
                provider=provider,
                surface="mcp_tool_descriptions",
                path="ncp/mcp/server.py::MCP_TOOLS",
                text=shared_text,
            )
        )
    return sources


def collect_provider_artifacts(
    repo_root: Path,
) -> dict[str, list[ProviderArtifact]]:
    """Return a provider-separated inventory with deterministic token accounting."""

    # Resolve the unit here as part of the inventory contract: callers can report
    # token_unit() alongside these counts without risking a different lazy setup.
    token_unit()
    result: dict[str, list[ProviderArtifact]] = {}
    for provider, sources in collect_provider_sources(repo_root).items():
        result[provider] = [
            ProviderArtifact(
                provider=source.provider,
                surface=source.surface,
                path=source.path,
                token_count=estimate_tokens(source.text),
                lifecycle_calls=_lifecycle_calls(source.text),
                trust_boundary_present=has_trust_boundary(source.text),
            )
            for source in sources
        ]
    return result
