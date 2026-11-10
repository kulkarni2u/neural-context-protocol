"""Baseline context strategies for benchmark comparisons."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from ncp.tokens import estimate_tokens


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


_TOKEN_RE = re.compile(r"\w+")


def _bm25_tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass(slots=True)
class PlainRagBaseline:
    """Ordinary BM25 top-k retrieval over raw transcript turns.

    Each prior transcript entry is one document. Documents are ranked by BM25
    against the current query and packed greedily, best first, into
    ``budget_tokens`` (a document that does not fit is skipped and the next one
    is tried). There is deliberately no trust weighting, recency, dedup,
    whispers, distillation or consolidation. Ties break by transcript order
    (earlier entry first) so output is deterministic.
    """

    budget_tokens: int = 400
    name: str = "plain_rag"

    def context_for(self, *, transcript: list[str], turn: str) -> str:
        return "\n".join(self.select(transcript=transcript, query=turn))

    def select(self, *, transcript: list[str], query: str) -> list[str]:
        if self.budget_tokens <= 0 or not transcript:
            return []
        from rank_bm25 import BM25Okapi

        query_tokens = _bm25_tokens(query)
        corpus = [_bm25_tokens(entry) or [""] for entry in transcript]
        if query_tokens:
            scores = [float(s) for s in BM25Okapi(corpus).get_scores(query_tokens)]
        else:
            scores = [0.0] * len(transcript)
        order = sorted(range(len(transcript)), key=lambda i: (-scores[i], i))

        selected: list[str] = []
        used = 0
        for index in order:
            entry = transcript[index]
            cost = estimate_tokens(entry)
            if used + cost > self.budget_tokens:
                continue
            selected.append(entry)
            used += cost
        return selected
