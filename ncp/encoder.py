"""Pidgin encoder for assembled NCP context blocks."""

from __future__ import annotations

import time
from collections.abc import Sequence

from .types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper


def _fmt_float(value: float) -> str:
    return f"{value:.2f}"


def _fmt_compact_list(values: Sequence[str]) -> str:
    return f"[{','.join(values)}]"


def _fmt_recent_list(values: Sequence[str]) -> str:
    return f"[{' | '.join(values)}]"


def _indent_block(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines() or [""])


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

        blocks = [
            self._encode_budget(budget),
            self._encode_conscious(conscious),
        ]

        if chunks:
            blocks.append(self._encode_subconscious(chunks))
        if whispers:
            blocks.append(self._encode_whispers(whispers, now=now))

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
            f"owns:{_fmt_compact_list(conscious.owns)} must-not:{_fmt_compact_list(conscious.must_not)}",
            f"task:{conscious.task}",
            (
                f"slot:{conscious.slot} "
                f"slot_age:{conscious.slot_age} "
                f"slot_conf:{_fmt_float(conscious.slot_confidence)}"
            ),
            f"intent:{conscious.intent}",
            f"goal_version:{conscious.goal_version}",
            f"recent:{_fmt_recent_list(conscious.recent)}",
            (
                f"tried:{_fmt_compact_list(conscious.tried)} "
                f"failed:{_fmt_compact_list(conscious.failed)}"
            ),
            f"drift_score:{_fmt_float(conscious.drift_score)}",
        ]
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
                        f"age:{age_seconds}s",
                    ]
                )
            )
            lines.append(_indent_block(whisper.payload))
        return "\n".join(lines)
