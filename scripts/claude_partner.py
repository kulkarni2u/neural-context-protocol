#!/usr/bin/env python3
"""Consume pending NCP whispers and run Claude as an implementation partner."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ncp.agent_handoff import (  # noqa: E402
    acknowledge_handoffs,
    emit_follow_up_whisper,
    load_sqlite_handoffs,
    run_claude_partner,
    truncate_whisper_payload,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Repository root for Claude access.")
    parser.add_argument("--agent-id", default="claude", help="Target agent id whose whispers should be consumed.")
    parser.add_argument("--pipeline-id", default=None, help="Optional pipeline filter.")
    parser.add_argument("--max-items", type=int, default=3, help="Maximum whisper handoffs to consume.")
    parser.add_argument("--min-confidence", type=float, default=0.60, help="Minimum whisper confidence.")
    parser.add_argument("--instruction", default=None, help="Optional extra instruction for Claude.")
    parser.add_argument("--emit-to", default=None, help="Optional follow-up whisper target.")
    parser.add_argument("--emit-type", default="share", help="Follow-up whisper type.")
    parser.add_argument("--emit-confidence", type=float, default=0.90, help="Follow-up whisper confidence.")
    parser.add_argument("--max-payload-chars", type=int, default=600, help="Follow-up whisper payload cap.")
    parser.add_argument("--timeout-seconds", type=float, default=90.0, help="Claude timeout budget.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    store, handoffs = load_sqlite_handoffs(
        cwd=args.cwd,
        agent_id=args.agent_id,
        pipeline_id=args.pipeline_id,
        max_items=args.max_items,
        min_confidence=args.min_confidence,
    )
    if not handoffs:
        raise SystemExit("No pending NCP handoffs for Claude.")

    response = run_claude_partner(
        cwd=args.cwd,
        agent_id=args.agent_id,
        handoffs=handoffs,
        instruction=args.instruction,
        timeout_seconds=args.timeout_seconds,
    )
    if args.emit_to:
        emit_follow_up_whisper(
            cwd=args.cwd,
            from_agent=args.agent_id,
            target=args.emit_to,
            pipeline_id=args.pipeline_id or handoffs[0].pipeline_id,
            payload=truncate_whisper_payload(response, max_chars=args.max_payload_chars),
            whisper_type=args.emit_type,
            confidence=args.emit_confidence,
        )
    acknowledge_handoffs(store, handoffs)
    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
