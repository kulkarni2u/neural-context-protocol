#!/usr/bin/env python3
"""Run a bounded Claude stream-json review and print the final JSON payload."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT_DIR / "ncp" / "claude_review_helper.py"
SPEC = importlib.util.spec_from_file_location("claude_review_helper", HELPER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - direct script bootstrap guard
    raise RuntimeError(f"Unable to load helper module from {HELPER_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
extract_json_object = MODULE.extract_json_object
extract_assistant_event_payload = MODULE.extract_assistant_event_payload
extract_text_event_payload = MODULE.extract_text_event_payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True, help="Path to the review prompt text file.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for the Claude process.")
    parser.add_argument("--model", default="sonnet", help="Claude model alias or full name.")
    parser.add_argument("--bare", action="store_true", help="Run Claude in bare mode.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0, help="Hard timeout for the review run.")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=2.0,
        help="Claude max budget for the run.",
    )
    parser.add_argument(
        "--json-schema",
        type=str,
        default=None,
        help="Optional JSON schema string for Claude structured output validation.",
    )
    parser.add_argument(
        "--disable-tools",
        action="store_true",
        help="Disable all Claude tools for the review run.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    prompt = args.prompt_file.read_text()
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        "Read,Glob,Grep",
        "--add-dir",
        str(args.cwd),
        "--model",
        args.model,
        "--max-budget-usd",
        str(args.max_budget_usd),
    ]
    if args.disable_tools:
        cmd.extend(["--tools", ""])
    if args.bare:
        cmd.append("--bare")
    if args.json_schema:
        cmd.extend(["--json-schema", args.json_schema])
    cmd.append("--")
    cmd.append(prompt)

    started_at = time.monotonic()
    process = subprocess.Popen(
        cmd,
        cwd=args.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    text_chunks: list[str] = []
    try:
        assert process.stdout is not None
        while True:
            if time.monotonic() - started_at > args.timeout_seconds:
                process.kill()
                stderr = process.stderr.read() if process.stderr else ""
                print(
                    json.dumps(
                        {
                            "status": "timeout",
                            "timeout_seconds": args.timeout_seconds,
                            "stderr": stderr.strip(),
                        }
                    ),
                    file=sys.stderr,
                )
                return 124

            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            text_payload = extract_text_event_payload(line)
            if text_payload is None:
                text_payload = extract_assistant_event_payload(line)
            if text_payload is None:
                continue
            text_chunks.append(text_payload)
            try:
                result = extract_json_object("\n".join(text_chunks))
            except Exception:
                continue
            print(json.dumps(result))
            process.terminate()
            return 0

        stderr = process.stderr.read() if process.stderr else ""
        raise RuntimeError(
            f"Claude stream-json review finished without a final JSON payload. stderr={stderr.strip()!r}"
        )
    finally:
        if process.poll() is None:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
