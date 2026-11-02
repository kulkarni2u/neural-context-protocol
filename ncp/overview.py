"""Cross-pipeline overview: ranking and ``[NCP:OVERVIEW]`` rendering (capability D).

The stores return raw per-pipeline facts (``BaseStore.overview_rows``). This
module ranks them by an attention score, renders a bounded wire block and a
structured twin of it. Everything here is read-only: no whisper is
acknowledged, no ``retrieval_count`` changes, no turn is logged.

Agent-written text (result summaries, task errors, decision choices) is data:
it is quoted, truncated, escaped and marked ``‹data›``, never rendered raw.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from typing import Any

from ncp.encoder import _fmt_age_bucket
from ncp.stores.base import BaseStore
from ncp.tokens import estimate_tokens

DEFAULT_SINCE_SECONDS = 86400.0
DEFAULT_MAX_PIPELINES = 10
MAX_PIPELINES_CEILING = 50
DEFAULT_MAX_TOKENS = 400
MIN_MAX_TOKENS = 60
MAX_MAX_TOKENS = 4000
SNIPPET_MAX_CHARS = 60
TOKEN_MAX_CHARS = 32
DATA_MARK = "‹data›"
HEADER = "[NCP:OVERVIEW]"

# Inbox whisper types that raise a pipeline's attention score.
ATTENTION_INBOX_TYPES = ("result", "alert", "dissent")
_INBOX_ORDER = ("alert", "dissent", "result")
_TASK_LETTERS = (("open", "o"), ("claimed", "c"), ("done", "d"), ("failed", "f"), ("cancelled", "x"))
_UNSAFE_TOKEN_CHARS = re.compile(r"[^A-Za-z0-9_.:/+-]")
_LINE_SEPARATORS = re.compile("[\u0085  ]")


def clamp_limits(max_pipelines: object, max_tokens: object) -> tuple[int, int]:
    """Clamp caller-supplied bounds to ``[1, 50]`` pipelines and ``[60, 4000]`` tokens."""

    try:
        pipelines = int(max_pipelines)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        pipelines = DEFAULT_MAX_PIPELINES
    try:
        tokens = int(max_tokens)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        tokens = DEFAULT_MAX_TOKENS
    return min(max(pipelines, 1), MAX_PIPELINES_CEILING), min(max(tokens, MIN_MAX_TOKENS), MAX_MAX_TOKENS)


def _truncate(text: str, limit: int) -> str:
    text = _LINE_SEPARATORS.sub(" ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def quote_data(text: str) -> str:
    """Quote agent-written text: truncated to 60 chars, escaped, one line, no data-mark forgery."""

    clean = _truncate(text, SNIPPET_MAX_CHARS).replace("‹", "<").replace("›", ">")
    return json.dumps(clean, ensure_ascii=False)


def _token(text: str, limit: int = TOKEN_MAX_CHARS) -> str:
    """Reduce agent-written text to an inert whitespace-free token."""

    return _UNSAFE_TOKEN_CHARS.sub("_", text)[:limit] or "_"


def _pipeline_label(pipeline_id: str) -> str:
    """Pipeline ids are identifiers callers pass back; quote only if they are not line-safe."""

    if pipeline_id and not any(ch.isspace() or ord(ch) < 32 for ch in pipeline_id):
        return pipeline_id
    return json.dumps(pipeline_id, ensure_ascii=False)


def attention_parts(row: dict[str, Any]) -> dict[str, int]:
    tasks = row.get("tasks") or {}
    inbox = row.get("inbox") or {}
    return {
        "failed": int(tasks.get("failed", 0)),
        "inbox": sum(int(inbox.get(kind, 0)) for kind in ATTENTION_INBOX_TYPES),
        "expiring_claims": int(row.get("expiring_claims", 0)),
        "stale_open": int(row.get("stale_open", 0)),
    }


def attention_score(parts: dict[str, int]) -> int:
    """``3*failed + 2*inbox(result|alert|dissent) + 2*expiring_claims + 1*stale_open``."""

    return 3 * parts["failed"] + 2 * parts["inbox"] + 2 * parts["expiring_claims"] + parts["stale_open"]


def rank_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows with ``attention``/``score`` filled in, best first."""

    ranked: list[dict[str, Any]] = []
    for row in rows:
        parts = attention_parts(row)
        ranked.append({**row, "attention": parts, "score": attention_score(parts)})
    ranked.sort(key=lambda r: (-r["score"], -float(r["last_activity"]), r["pipeline_id"]))
    return ranked


def _last_text(row: dict[str, Any]) -> str | None:
    last = row.get("last_result")
    if not last:
        return None
    status = _token(str(last.get("status") or "result"), 16)
    summary = " ".join(str(last.get("summary") or "").split())
    return f"{status}: {summary}" if summary else status


def _row_lines(row: dict[str, Any], now: float) -> tuple[str, str | None]:
    age = _fmt_age_bucket(max(0, int(now - float(row["last_activity"]))))
    parts = [f"p:{_pipeline_label(row['pipeline_id'])}", f"act:{age}"]
    tasks = row.get("tasks")
    if tasks is not None:
        letters = "/".join(f"{letter}{tasks[name]}" for name, letter in _TASK_LETTERS if tasks.get(name))
        if letters:
            parts.append(f"tasks:{letters}")
    attn = row["attention"]
    flags = []
    if attn["failed"]:
        flags.append(f"f{attn['failed']}")
    if attn["expiring_claims"]:
        flags.append(f"exp{attn['expiring_claims']}")
    if attn["stale_open"]:
        flags.append(f"stale{attn['stale_open']}")
    if flags:
        parts.append("attn:" + ",".join(flags))
    inbox = row.get("inbox") or {}
    if inbox:
        ordered = [k for k in _INBOX_ORDER if k in inbox] + sorted(k for k in inbox if k not in _INBOX_ORDER)
        parts.append("inbox:" + ",".join(f"{_token(k, 16)}{inbox[k]}" for k in ordered))
    decision = row.get("last_decision")
    if decision:
        parts.append(f"dec:{_token(str(decision['schema_id']))}={_token(str(decision['choice']))}")
    if row.get("drift") is not None:
        parts.append(f"drift:{float(row['drift']):.2f}")
    if row.get("dissent_24h"):
        parts.append(f"dissent24h:{int(row['dissent_24h'])}")
    text = _last_text(row)
    last_line = None if text is None else f"  last:{quote_data(text)} {DATA_MARK}"
    return " ".join(parts), last_line


def _structured(row: dict[str, Any], now: float) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pipeline_id": row["pipeline_id"],
        "last_activity": float(row["last_activity"]),
        "age_seconds": max(0, int(now - float(row["last_activity"]))),
        "score": row["score"],
        "attention": dict(row["attention"]),
        "inbox": dict(row.get("inbox") or {}),
        "last_result": None,
        "last_decision": None,
        "drift": row.get("drift"),
        "dissent_24h": int(row.get("dissent_24h") or 0),
    }
    if row.get("tasks") is not None:
        out["tasks"] = dict(row["tasks"])
    last = row.get("last_result")
    if last:
        out["last_result"] = {
            "source": last.get("source"),
            "status": _token(str(last.get("status") or "result"), 16),
            "summary": _truncate(" ".join(str(last.get("summary") or "").split()), SNIPPET_MAX_CHARS),
            "data": True,
        }
    decision = row.get("last_decision")
    if decision:
        out["last_decision"] = {
            "schema_id": _token(str(decision["schema_id"])),
            "choice": _token(str(decision["choice"])),
        }
    return out


def render_overview(
    rows: Sequence[dict[str, Any]],
    *,
    now: float | None = None,
    max_pipelines: int = DEFAULT_MAX_PIPELINES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Rank ``rows`` and render the largest prefix that fits ``max_tokens``.

    The ``+N more`` line counts against the budget. Returns
    ``{"text", "pipelines", "omitted", "tokens_est"}``.
    """

    now = time.time() if now is None else float(now)
    max_pipelines, max_tokens = clamp_limits(max_pipelines, max_tokens)
    ranked = rank_rows(rows)
    total = len(ranked)
    lines: list[str] = [HEADER]
    shown: list[dict[str, Any]] = []

    for row in ranked[:max_pipelines]:
        left = total - (len(shown) + 1)
        tail = [f"+{left} more"] if left > 0 else []
        head, last = _row_lines(row, now)
        for variant in ([head, last] if last else [head], [head]):
            trial = [*lines, *variant, *tail]
            if estimate_tokens("\n".join(trial)) <= max_tokens:
                lines.extend(variant)
                shown.append(row)
                break
        else:
            break

    omitted = total - len(shown)
    if total == 0:
        lines.append("(no active pipelines)")
    elif omitted > 0:
        lines.append(f"+{omitted} more")
    text = "\n".join(lines)
    return {
        "text": text,
        "pipelines": [_structured(row, now) for row in shown],
        "omitted": omitted,
        "tokens_est": estimate_tokens(text),
    }


def overview(
    store: BaseStore,
    *,
    viewer: str | None = None,
    pipelines: Sequence[str] | None = None,
    pipeline_prefix: str | None = None,
    since_seconds: float | None = DEFAULT_SINCE_SECONDS,
    max_pipelines: int = DEFAULT_MAX_PIPELINES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    include_tasks: bool = True,
    stale_open_seconds: float = 1800.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the cross-pipeline overview from ``store`` (read-only)."""

    now = time.time() if now is None else float(now)
    rows = store.overview_rows(
        pipelines=pipelines,
        pipeline_prefix=pipeline_prefix,
        since=since_seconds,
        viewer=viewer,
        now=now,
        include_tasks=include_tasks,
        stale_open_seconds=stale_open_seconds,
    )
    return render_overview(rows, now=now, max_pipelines=max_pipelines, max_tokens=max_tokens)


async def async_overview(
    store: BaseStore,
    *,
    viewer: str | None = None,
    pipelines: Sequence[str] | None = None,
    pipeline_prefix: str | None = None,
    since_seconds: float | None = DEFAULT_SINCE_SECONDS,
    max_pipelines: int = DEFAULT_MAX_PIPELINES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    include_tasks: bool = True,
    stale_open_seconds: float = 1800.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Async counterpart of :func:`overview` using ``async_overview_rows``."""

    now = time.time() if now is None else float(now)
    rows = await store.async_overview_rows(
        pipelines=pipelines,
        pipeline_prefix=pipeline_prefix,
        since=since_seconds,
        viewer=viewer,
        now=now,
        include_tasks=include_tasks,
        stale_open_seconds=stale_open_seconds,
    )
    return render_overview(rows, now=now, max_pipelines=max_pipelines, max_tokens=max_tokens)
