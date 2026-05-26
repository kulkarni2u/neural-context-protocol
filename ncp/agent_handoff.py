"""Thin partner/reviewer wrappers that consume NCP whisper handoffs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Protocol

from ncp.api import configure, emit
from ncp.claude_review_helper import extract_json_object
from ncp.dogfood import _extract_opencode_text
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import Whisper

DEFAULT_CLAUDE_PARTNER_INSTRUCTION = (
    "Use the NCP handoff below as your primary context. Work only inside the bound repo, "
    "stay concise, and focus on implementing or unblocking the requested slice."
)

DEFAULT_OPENCODE_REVIEW_INSTRUCTION = (
    "Review the NCP handoff below. Findings come first. Focus on correctness, regressions, "
    "and missing tests. Be concise."
)


class HandoffStore(Protocol):
    """Kept for backward compatibility — BaseStore now declares both methods."""

    def peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]: ...

    def acknowledge_whispers(self, whisper_ids: list[str]) -> int: ...


def load_handoffs(
    *,
    cwd: Path,
    agent_id: str,
    pipeline_id: str | None = None,
    max_items: int = 3,
    min_confidence: float = 0.60,
) -> tuple[BaseStore, list[Whisper]]:
    """Load pending whisper handoffs without consuming them."""

    config = configure(cwd=cwd)
    store = create_store(config)
    handoffs = store.peek_whispers(
        agent_id=agent_id,
        pipeline_id=pipeline_id,
        max_items=max_items,
        min_confidence=min_confidence,
    )
    return store, handoffs


def format_handoffs(handoffs: list[Whisper]) -> str:
    """Render whisper handoffs into a compact prompt block."""

    if not handoffs:
        return "No pending handoffs."
    lines: list[str] = []
    for whisper in handoffs:
        pipeline = whisper.pipeline_id or "global"
        lines.append(
            " | ".join(
                [
                    f"id={whisper.whisper_id}",
                    f"pipeline={pipeline}",
                    f"from={whisper.from_agent}",
                    f"type={whisper.whisper_type}",
                    f"confidence={whisper.confidence:.2f}",
                ]
            )
        )
        lines.append(f"payload={whisper.payload}")
    return "\n".join(lines)


def build_claude_partner_prompt(
    *,
    cwd: Path,
    handoffs: list[Whisper],
    instruction: str | None = None,
) -> str:
    """Build the default Claude implementation-partner prompt."""

    return "\n\n".join(
        [
            f"Repository root: {cwd}",
            instruction or DEFAULT_CLAUDE_PARTNER_INSTRUCTION,
            "NCP handoff(s):",
            format_handoffs(handoffs),
        ]
    )


def build_opencode_review_prompt(
    *,
    cwd: Path,
    handoffs: list[Whisper],
    instruction: str | None = None,
) -> str:
    """Build the default OpenCode reviewer prompt."""

    return "\n\n".join(
        [
            f"Repository root: {cwd}",
            instruction or DEFAULT_OPENCODE_REVIEW_INSTRUCTION,
            "NCP handoff(s):",
            format_handoffs(handoffs),
            (
                "Respond with JSON only: "
                '{"verdict":"pass|needs_fix","findings":["..."],'
                '"recommended_next_steps":["..."],"summary":"..."}'
            ),
        ]
    )


def truncate_whisper_payload(text: str, *, max_chars: int) -> str:
    """Keep follow-up whisper payloads bounded."""

    max_chars = min(max_chars, 600)
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def emit_follow_up_whisper(
    *,
    cwd: Path,
    from_agent: str,
    target: str,
    pipeline_id: str | None,
    payload: str,
    whisper_type: str = "share",
    confidence: float = 0.9,
) -> None:
    """Emit one bounded follow-up whisper."""

    config = configure(cwd=cwd)
    store = create_store(config)
    emit(
        Whisper(
            from_agent=from_agent,
            target=target,
            whisper_type=whisper_type,
            payload=payload,
            confidence=confidence,
            pipeline_id=pipeline_id,
        ),
        store=store,
    )


def run_claude_partner(
    *,
    cwd: Path,
    agent_id: str,
    handoffs: list[Whisper],
    instruction: str | None = None,
    command: list[str] | None = None,
    timeout_seconds: float = 90.0,
) -> str:
    """Run the repo-bound Claude implementation-partner path."""

    prompt = build_claude_partner_prompt(cwd=cwd, handoffs=handoffs, instruction=instruction)
    completed = subprocess.run(
        command
        or [
            "claude",
            "-p",
            "--model",
            "sonnet",
            "--dangerously-skip-permissions",
            "--add-dir",
            str(cwd),
            "--",
            prompt,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Claude partner run failed")
    return completed.stdout.strip()


def run_opencode_reviewer(
    *,
    cwd: Path,
    agent_id: str,
    handoffs: list[Whisper],
    instruction: str | None = None,
    command: list[str] | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """Run the repo-bound OpenCode review path."""

    prompt = build_opencode_review_prompt(cwd=cwd, handoffs=handoffs, instruction=instruction)
    completed = subprocess.run(
        command
        or [
            "opencode",
            "run",
            "-m",
            "opencode/deepseek-v4-flash-free",
            "--format",
            "json",
            "--dir",
            str(cwd),
            prompt,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "OpenCode review run failed")
    return _extract_opencode_text(completed.stdout)


def acknowledge_handoffs(store: BaseStore, handoffs: list[Whisper]) -> int:
    """Delete handoffs after a successful consumer run."""

    return store.acknowledge_whispers([whisper.whisper_id for whisper in handoffs])


def parse_json_review(text: str) -> dict[str, object]:
    """Parse OpenCode's JSON review payload."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = extract_json_object(text)
    if not isinstance(payload, dict):
        raise ValueError("OpenCode reviewer payload must be a JSON object")
    return payload
