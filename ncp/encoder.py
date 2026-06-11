"""Pidgin encoder for assembled NCP context blocks."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence

from .types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper


def _fmt_float(value: float) -> str:
    return f"{value:.1f}"


def _fmt_compact_list(values: Sequence[str]) -> str:
    return f"[{','.join(values)}]"


def _fmt_recent_list(values: Sequence[str]) -> str:
    return f"[{' | '.join(values)}]"


def _indent_block(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines() or [""])


def _fmt_age_bucket(age_seconds: int) -> str:
    if age_seconds < 60:
        return "<1m"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h"
    return f"{age_seconds // 86400}d"


def _fmt_payload_value(value: object) -> str:
    if isinstance(value, list):
        return f"[{','.join(str(item) for item in value)}]"
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


class PidginEncoder:
    """Encode assembled context into the V1 NCP pidgin wire format."""

    def assemble(
        self,
        conscious: ConsciousBlock,
        chunks: Sequence[SubconsciousChunk],
        whispers: Sequence[Whisper],
        budget: BudgetContext,
        *,
        now: float | None = None,
    ) -> str:
        """Assemble the wire-format block ordering for one provider turn."""

        blocks = [self._encode_conscious(conscious)]

        if chunks:
            blocks.append(self._encode_subconscious(chunks))
        if whispers:
            blocks.append(self._encode_whispers(whispers, now=now))
        blocks.append(self._encode_budget(budget))

        return "\n\n".join(blocks)

    def _encode_budget(self, budget: BudgetContext) -> str:
        steps_total = "?" if budget.steps_total is None else str(budget.steps_total)
        elapsed_seconds = int(round(budget.elapsed_seconds))
        return (
            "[NCP:BUDGET] "
            f"ctx_used:{_fmt_float(budget.ctx_used)} "
            f"steps:{budget.steps_completed}/{steps_total} "
            f"elapsed:{elapsed_seconds}s "
            f"pressure:{budget.pressure}"
        )

    def _encode_conscious(self, conscious: ConsciousBlock) -> str:
        lines = [
            "[NCP:CONSCIOUS]",
            f"id:{conscious.agent_id} role:{conscious.role} ncp_v:{conscious.ncp_v}",
            f"task:{conscious.task}",
            f"slot:{conscious.slot}",
            f"intent:{conscious.intent}",
        ]
        if conscious.owns:
            lines.append(f"owns:{_fmt_compact_list(conscious.owns)}")
        if conscious.must_not:
            lines.append(f"must-not:{_fmt_compact_list(conscious.must_not)}")
        slot_bits = []
        if conscious.slot_age:
            slot_bits.append(f"slot_age:{conscious.slot_age}")
        if conscious.slot_confidence < 1.0:
            slot_bits.append(f"slot_conf:{_fmt_float(conscious.slot_confidence)}")
        if slot_bits:
            lines.append(" ".join(slot_bits))
        if conscious.goal_version != 1:
            lines.append(f"goal_version:{conscious.goal_version}")
        if conscious.recent:
            lines.append(f"recent:{_fmt_recent_list(conscious.recent)}")
        if conscious.tried:
            lines.append(f"tried:{_fmt_compact_list(conscious.tried)}")
        if conscious.failed:
            lines.append(f"failed:{_fmt_compact_list(conscious.failed)}")
        if conscious.drift_score > 0.0:
            lines.append(f"drift_score:{_fmt_float(conscious.drift_score)}")
        return "\n".join(lines)

    def _encode_subconscious(self, chunks: Sequence[SubconsciousChunk]) -> str:
        lines = ["[NCP:SUBCONSCIOUS]"]
        for chunk in chunks:
            lines.append(
                " ".join(
                    [
                        f"chunk:{chunk.chunk_id}",
                        f"layer:{chunk.layer}",
                        f"score:{_fmt_float(chunk.effective_score)}",
                        f"src:{chunk.src}",
                        f"trust:{_fmt_float(chunk.base_trust)}",
                    ]
                )
            )
            lines.append(_indent_block(chunk.content))
        return "\n".join(lines)

    def _encode_whispers(self, whispers: Sequence[Whisper], *, now: float | None) -> str:
        effective_now = time.time() if now is None else now
        lines = ["[NCP:WHISPERS]"]
        for whisper in whispers:
            age_seconds = max(0, int(round(effective_now - whisper.created_at)))
            lines.append(
                " ".join(
                    [
                        "wsp",
                        f"from:{whisper.from_agent}",
                        f"to:{whisper.target}",
                        f"t:{whisper.whisper_type}",
                        f"c:{_fmt_float(whisper.confidence)}",
                        f"age:{_fmt_age_bucket(age_seconds)}",
                    ]
                )
            )
            lines.extend(self._encode_whisper_payload_lines(whisper.payload))
        return "\n".join(lines)

    def _encode_whisper_payload_lines(self, payload: str) -> list[str]:
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return [_indent_block(payload)]
        if not isinstance(parsed, dict):
            return [_indent_block(str(parsed))]
        return [f"  {key}:{_fmt_payload_value(value)}" for key, value in parsed.items() if value not in (None, "", [])]
