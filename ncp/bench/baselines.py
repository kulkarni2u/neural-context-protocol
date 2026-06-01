"""Baseline context strategies for benchmark comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BaselineStrategy(Protocol):
    """Context-building strategy used as a benchmark comparison."""

    name: str

    def context_for(self, *, transcript: list[str], turn: str) -> str: ...


@dataclass(slots=True)
class RawReplayBaseline:
    """Replay the entire transcript every turn."""

    name: str = "raw_replay"

    def context_for(self, *, transcript: list[str], turn: str) -> str:
        return "\n".join(transcript)


@dataclass(slots=True)
class SlidingWindowBaseline:
    """Keep only the most recent N transcript entries."""

    last_entries: int = 8
    name: str = "sliding_window"

    def context_for(self, *, transcript: list[str], turn: str) -> str:
        if self.last_entries <= 0:
            return ""
        return "\n".join(transcript[-self.last_entries :])


@dataclass(slots=True)
class RollingSummaryBaseline:
    """Compact older transcript entries into a deterministic summary."""

    every_k: int = 4
    keep_recent: int = 4
    name: str = "rolling_summary"

    def context_for(self, *, transcript: list[str], turn: str) -> str:
        if not transcript:
            return ""
        keep_recent = max(0, self.keep_recent)
        if len(transcript) <= keep_recent:
            return "\n".join(transcript)

        older = transcript[:-keep_recent] if keep_recent > 0 else transcript
        recent = transcript[-keep_recent:]
        summary_lines = self._summarize(older)
        return "\n".join([*summary_lines, *recent]).strip()

    def _summarize(self, entries: list[str]) -> list[str]:
        if not entries:
            return []
        group_size = max(1, self.every_k)
        summaries: list[str] = []
        for index in range(0, len(entries), group_size):
            group = entries[index : index + group_size]
            clipped = []
            for entry in group:
                normalized = " ".join(entry.split())
                if len(normalized) > 96:
                    normalized = normalized[:95].rstrip() + "…"
                clipped.append(normalized)
            summaries.append(f"SUMMARY[{index // group_size + 1}]: " + " || ".join(clipped))
        return summaries
