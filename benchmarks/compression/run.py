#!/usr/bin/env python3
"""Ingestion-time content compression benchmark for NCP.

This benchmark measures NCP's deterministic ingestion-time noise reduction:
the ``filter_content`` filter applied in the ``ncp_write_memory`` MCP handler
before a tool result is stored as a memory chunk.

It is fully deterministic and requires NO network and NO API keys. A fixed
corpus of hand-authored but realistic NOISY payloads -- the kind of output
real agents actually write to memory -- is run through ``filter_content`` and
the per-payload and aggregate char/token reduction is reported.

What it measures: deterministic framing-noise reduction on representative
noisy inputs (ANSI color codes, progress bars, timing lines, consecutive
duplicate log lines, null/empty JSON fields, runs of blank lines).

What it does NOT measure: model quality, retrieval quality, or semantic
compression. The filter is lossless in intent -- it removes framing, not
signal -- so the numbers here are an honest floor on how much raw tool noise
NCP strips before storage, not a claim about summarization.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from ncp.chunker import filter_content
from ncp.tokens import estimate_tokens, token_unit


@dataclass(slots=True, frozen=True)
class CompressionPayload:
    """A single named, hand-authored noisy payload in the benchmark corpus."""

    payload_id: str
    category: str
    description: str
    content: str


# ---------------------------------------------------------------------------
# Fixed, deterministic corpus
#
# Every payload is hand-authored and stable. The escape byte ``\x1b`` is used
# directly so the ANSI-stripping path is exercised exactly as in production.
# ---------------------------------------------------------------------------

_ESC = "\x1b"

# (a) Tool/CLI output: ANSI color codes + progress bars + real/user/sys timing.
_CLI_OUTPUT = (
    f"{_ESC}[1m$ pip install -r requirements.txt{_ESC}[0m\n"
    f"{_ESC}[32mCollecting numpy{_ESC}[0m\n"
    "Downloading numpy-2.1.0-cp312-cp312-manylinux.whl (18.3 MB)\n"
    "  0% |                                        | 0.0/18.3 MB\n"
    " 12% |█████                                   | 2.2/18.3 MB\n"
    " 38% |███████████████                         | 7.0/18.3 MB\n"
    " 71% |████████████████████████████            | 13.0/18.3 MB\n"
    "100% |████████████████████████████████████████| 18.3/18.3 MB\n"
    f"{_ESC}[32mCollecting scipy{_ESC}[0m\n"
    "  0% |                                        | 0.0/34.1 MB\n"
    " 50% |████████████████████                    | 17.0/34.1 MB\n"
    "100% |████████████████████████████████████████| 34.1/34.1 MB\n"
    f"{_ESC}[1;32mSuccessfully installed numpy-2.1.0 scipy-1.14.0{_ESC}[0m\n"
    "\n"
    "real    0m12.481s\n"
    "user    0m9.204s\n"
    "sys     0m1.872s\n"
)

# (b) Verbose log with many consecutive duplicate lines.
_RETRY_LINE = "WARN  connection to db-primary:5432 refused, retrying in 2s"
_VERBOSE_LOG = (
    "INFO  starting worker pool (size=8)\n"
    "INFO  attempting database connection\n"
    + (_RETRY_LINE + "\n") * 9
    + "INFO  connection established to db-replica:5432\n"
    "DEBUG cache miss for key user:42\n"
    "DEBUG cache miss for key user:42\n"
    "DEBUG cache miss for key user:42\n"
    "INFO  request handled in 38ms\n"
)

# (c) Null-heavy / empty-field JSON tool result.
_JSON_RESULT = json.dumps(
    {
        "status": "ok",
        "id": "run_8821",
        "error": None,
        "warnings": [],
        "stderr": "",
        "stdout": "build complete",
        "duration_ms": 1842,
        "artifact_url": None,
        "metadata": None,
        "tags": [],
        "retries": 0,
    },
    indent=2,
)

# (d) Mixed stack-trace-style blob with blank-line runs.
_STACK_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "app/handler.py", line 88, in dispatch\n'
    "    return route(request)\n"
    "\n"
    "\n"
    "\n"
    '  File "app/router.py", line 41, in route\n'
    "    handler = self._resolve(path)\n"
    "\n"
    "\n"
    "\n"
    "\n"
    '  File "app/router.py", line 57, in _resolve\n'
    "    raise KeyError(path)\n"
    "KeyError: '/v2/orders'\n"
    "\n"
    "\n"
    "\n"
    "During handling of the above exception, another exception occurred:\n"
    "\n"
    "\n"
    "\n"
    "RuntimeError: unhandled route '/v2/orders'\n"
)


CORPUS: tuple[CompressionPayload, ...] = (
    CompressionPayload(
        payload_id="cli_ansi_progress",
        category="cli_output",
        description="Package-install CLI output with ANSI color codes, progress "
        "bars, and real/user/sys timing lines.",
        content=_CLI_OUTPUT,
    ),
    CompressionPayload(
        payload_id="verbose_retry_log",
        category="duplicate_log",
        description="Verbose worker log with a long run of identical retry "
        "warnings plus repeated cache-miss lines.",
        content=_VERBOSE_LOG,
    ),
    CompressionPayload(
        payload_id="json_null_empty",
        category="json_result",
        description="Tool result JSON with null and empty-collection fields that "
        "carry no signal.",
        content=_JSON_RESULT,
    ),
    CompressionPayload(
        payload_id="stacktrace_blank_runs",
        category="stack_trace",
        description="Python stack trace padded with runs of 3+ blank lines "
        "between frames.",
        content=_STACK_TRACE,
    ),
)


def _measure(payload: CompressionPayload) -> dict[str, object]:
    """Run a single payload through filter_content and measure reduction."""

    result = filter_content(payload.content)
    raw_chars = result.raw_len
    filtered_chars = result.filtered_len
    raw_tokens = estimate_tokens(payload.content)
    filtered_tokens = estimate_tokens(result.filtered)

    char_reduction = result.reduction_ratio
    token_reduction = 0.0 if raw_tokens == 0 else 1.0 - (filtered_tokens / raw_tokens)

    return {
        "payload_id": payload.payload_id,
        "category": payload.category,
        "description": payload.description,
        "was_filtered": result.was_filtered,
        "raw_chars": raw_chars,
        "filtered_chars": filtered_chars,
        "char_reduction_ratio": round(char_reduction, 4),
        "raw_tokens": raw_tokens,
        "filtered_tokens": filtered_tokens,
        "token_reduction_ratio": round(token_reduction, 4),
    }


def run_compression_benchmark(
    *,
    pass_threshold: float = 0.20,
    corpus: tuple[CompressionPayload, ...] = CORPUS,
) -> dict[str, object]:
    """Measure NCP ingestion-time content compression over a fixed corpus.

    Args:
        pass_threshold: Minimum aggregate token reduction required for the
            ``pass`` gate to be True. Set conservatively below the measured
            result so the gate is a real, passing floor.
        corpus: The corpus of noisy payloads to measure. Defaults to the fixed
            benchmark corpus.

    Returns:
        A JSON-serializable artifact dict with config, per-payload rows, and an
        aggregate summary including a ``pass`` gate.
    """

    rows = [_measure(payload) for payload in corpus]

    total_raw_chars = sum(int(row["raw_chars"]) for row in rows)
    total_filtered_chars = sum(int(row["filtered_chars"]) for row in rows)
    total_raw_tokens = sum(int(row["raw_tokens"]) for row in rows)
    total_filtered_tokens = sum(int(row["filtered_tokens"]) for row in rows)

    agg_char_reduction = (
        0.0 if total_raw_chars == 0 else 1.0 - (total_filtered_chars / total_raw_chars)
    )
    agg_token_reduction = (
        0.0 if total_raw_tokens == 0 else 1.0 - (total_filtered_tokens / total_raw_tokens)
    )

    by_category: dict[str, dict[str, float]] = {}
    for row in rows:
        by_category[str(row["category"])] = {
            "char_reduction_ratio": float(row["char_reduction_ratio"]),
            "token_reduction_ratio": float(row["token_reduction_ratio"]),
        }

    passed = agg_token_reduction >= pass_threshold and total_filtered_tokens < total_raw_tokens

    return {
        "benchmark": "content_compression",
        "token_unit": token_unit(),
        "config": {
            "pass_threshold": pass_threshold,
            "payload_count": len(rows),
        },
        "payloads": rows,
        "summary": {
            "total_raw_chars": total_raw_chars,
            "total_filtered_chars": total_filtered_chars,
            "aggregate_char_reduction_ratio": round(agg_char_reduction, 4),
            "total_raw_tokens": total_raw_tokens,
            "total_filtered_tokens": total_filtered_tokens,
            "aggregate_token_reduction_ratio": round(agg_token_reduction, 4),
            "by_category": by_category,
            "pass_threshold": pass_threshold,
            "pass": passed,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=0.20,
        help="Minimum aggregate token reduction required for the pass gate.",
    )
    args = parser.parse_args()

    artifact = run_compression_benchmark(pass_threshold=args.pass_threshold)
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
