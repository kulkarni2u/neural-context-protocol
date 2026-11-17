#!/usr/bin/env python3
"""Harness and subagent overhead benchmark (deterministic, in-process, mock provider).

Structural character counts, unit ``chars_div4``: no network, no provider tokens, no
dollars. Every "before" is the new code with the previous behavior switched on
(``--return full``, explicit ids + ``result_full``, ``standard`` profile,
``delta:false``, ``show_score=true``, inline skill text), not a replay of the old
release. See docs/NCP_BENCHMARK_HARNESS_OVERHEAD.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from click.testing import CliRunner

from ncp.assembler import Assembler
from ncp.cli import main as ncp_cli
from ncp.config import load_config
from ncp.encoder import PidginEncoder
from ncp.mcp.server import MCP_TOOLS, _handle_request, make_handlers, tools_for_config
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_budget_tokens, estimate_tokens, token_unit
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper

PROFILES = ("minimal", "core", "standard", "full")
_REAL_TIME = time.time


def _compact(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def _ratio(after: float, before: float) -> float:
    return round(after / before, 4) if before else 0.0


@contextmanager
def _token_unit_pinned() -> Iterator[None]:
    """Pin the deterministic chars_div4 unit so the artifact does not depend on tiktoken."""

    from ncp.tokens import reset_encoder_cache

    previous = os.environ.get("NCP_TOKEN_UNIT")
    os.environ["NCP_TOKEN_UNIT"] = "chars_div4"
    reset_encoder_cache()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("NCP_TOKEN_UNIT", None)
        else:
            os.environ["NCP_TOKEN_UNIT"] = previous
        reset_encoder_cache()


def _call(handlers: dict, name: str, arguments: dict) -> dict:
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        handlers,
        tools=MCP_TOOLS,
    )
    payload = json.loads(raw)
    if "error" in payload:
        raise RuntimeError(f"{name} failed: {payload['error']}")
    return json.loads(payload["result"]["content"][0]["text"])


# --- a. tool catalog per profile -------------------------------------------------------


def _catalog(workdir: Path) -> dict[str, Any]:
    def chars(profile: str, *, skills: bool) -> tuple[int, int]:
        root = workdir / f"catalog_{profile}_{int(skills)}"
        (root / ".ncp").mkdir(parents=True)
        toml = f'[tools]\nprofile = "{profile}"\n[tasks]\nenabled = true\n[memoization]\nenabled = true\n'
        if skills:
            toml += "[skill_cache]\nenabled = true\n"
        (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")
        tools = tools_for_config(load_config(cwd=root))
        return _compact(tools), len(tools)

    rows = {p: chars(p, skills=False) for p in PROFILES}
    full_skills = chars("full", skills=True)
    return {
        "chars": {**{p: rows[p][0] for p in PROFILES}, "full_with_skills": full_skills[0]},
        "tool_counts": {**{p: rows[p][1] for p in PROFILES}, "full_with_skills": full_skills[1]},
        "before_profile": "standard",
        "after_profile": "minimal",
        "before": rows["standard"][0],
        "after": rows["minimal"][0],
        "note": "before = standard (the default profile), after = a minimal connection; tasks and memo tools enabled",
    }


# --- b. model-emitted characters per protocol turn -------------------------------------


def _protocol_args(workdir: Path) -> dict[str, Any]:
    store = SQLiteStore(workdir / "protocol.db")
    for i in range(6):
        store.write(SubconsciousChunk(
            chunk_id=f"fact{i}", layer="semantic", src="tool_result", pipeline_id="p", written_by=f"w{i}",
            content=f"payment retry fact {i}: " + "backoff jitter detail " * 12,
        ))
    store.emit_whisper(Whisper(
        from_agent="planner", target="builder", whisper_type="share", payload={"ask": "check backoff"},
        confidence=0.9, pipeline_id="p", ttl_seconds=3600,
    ))
    handlers = make_handlers(store)
    base = {"agent_id": "builder", "role": "build", "task": "payment_retry", "slot": "retry",
            "intent": "fix", "pipeline_id": "p", "session_id": "bench_protocol"}
    ctx = _call(handlers, "ncp_get_context", base)
    summary = "Added capped exponential backoff with jitter to the retry loop; tests green."
    full = (summary + " ") * 20  # ~1.6 KB answer the harness already holds
    common = {**base, "turn_id": "turn_before", "result_summary": summary}
    before_args = {
        **common,
        "result_full": full.strip(),
        "retrieved_chunk_ids": ctx["retrieved_chunk_ids"],
        "ack_whisper_ids": ctx.get("pending_whisper_ids", []),
    }
    after_args = {**common, "turn_id": "turn_after"}
    # Both shapes are accepted by the server; the after shape credits the same ids server-side.
    before_ok = _call(handlers, "ncp_post_turn", before_args)["posted"]
    ctx2 = _call(handlers, "ncp_get_context", base)
    after_ok = _call(handlers, "ncp_post_turn", after_args)["posted"]
    return {
        "before": _compact(before_args),
        "after": _compact(after_args),
        "retrieved_ids_explicit": len(before_args["retrieved_chunk_ids"]),
        "whisper_ids_explicit": len(before_args["ack_whisper_ids"]),
        "result_full_chars": len(before_args["result_full"]),
        "both_accepted": bool(before_ok and after_ok and ctx2["retrieved_chunk_ids"]),
        "note": "before = explicit ids + result_full, after = ids omitted (server-held) + summary only",
    }


# --- c/g. handoff return and skill dispatch --------------------------------------------


def _init_project(root: Path, extra_toml: str = "") -> SQLiteStore:
    root.mkdir(parents=True, exist_ok=True)
    CliRunner().invoke(ncp_cli, ["init", "--cwd", str(root)])
    if extra_toml:
        config_path = root / ".ncp" / "config.toml"
        config_path.write_text(config_path.read_text(encoding="utf-8") + "\n" + extra_toml, encoding="utf-8")
    return SQLiteStore(root / ".ncp" / "store.db")


def _handoff_return(workdir: Path, response_chars: int) -> dict[str, Any]:
    pipeline = "pipe_overhead"
    root = workdir / "handoff"
    results: dict[str, int] = {}
    for mode in ("full", "summary"):
        store = _init_project(root / mode)
        store.emit_whisper(Whisper(
            from_agent="codex", target="claude", whisper_type="nudge", payload="do the thing",
            confidence=0.95, pipeline_id=pipeline,
        ))
        response = ("word " * (response_chars // 5)).strip()
        with patch("ncp.agent_handoff.run_claude_partner", lambda **_: response):
            run = CliRunner().invoke(
                ncp_cli,
                ["handoff", "claude", "--cwd", str(root / mode), "--pipeline-id", pipeline, "--return", mode],
            )
        if run.exit_code != 0:
            raise RuntimeError(f"handoff --return {mode} failed: {run.output}")
        results[mode] = len(run.output.rstrip("\n"))
    return {
        "provider_response_chars": response_chars,
        "before": results["full"],
        "after": results["summary"],
        "note": "before = --return full, after = default summary (full text stays fetchable by chunk id)",
    }


_SKILL_STEPS = "\n".join(
    f"Step {i}: run the checks for stage {i}, record the outcome, and stop on the first failure." for i in range(1, 70)
)


def _skill_dispatch(workdir: Path, workers: int, skill_chars: int) -> dict[str, Any]:
    from ncp.agent_handoff import resolve_skill_prefix
    from ncp.skill_cache import cache_skill

    root = workdir / "skill"
    pipeline = "pipe_skill"
    store = _init_project(root, "[skill_cache]\nenabled = true\n")
    skill_text = ("# Deploy workflow\n" + _SKILL_STEPS)[:skill_chars]
    config = load_config(cwd=root)
    cache_skill(skill_text, skill_id="team/deploy", store=store, config=config, pipeline_id=pipeline,
                kind="workflow", registered_by="user")
    prefix = resolve_skill_prefix(["team/deploy"], cwd=root, pipeline_id=pipeline)
    header = re.match(r"\[NCP:SKILL\] skill:(team/deploy@[0-9a-f]{12}) kind:workflow\n", prefix)
    if header is None or not prefix.endswith(skill_text):
        raise RuntimeError("by-reference dispatch did not deliver the registered skill text to the worker")

    command = f"ncp handoff claude --pipeline-id {pipeline}"
    inline = f'{command} --instruction "{skill_text}"'
    by_ref = f"{command} --skill team/deploy"
    return {
        "workers": workers,
        "skill_chars": len(skill_text),
        "before": len(inline) * workers,
        "after": len(by_ref) * workers,
        "per_dispatch_before": len(inline),
        "per_dispatch_after": len(by_ref),
        "worker_receives_skill_text": True,
        "resolved_ref": header.group(1),
        "note": "orchestrator-emitted dispatch text only; the worker still receives the skill text in its prompt",
    }


# --- d. cross-turn delta ---------------------------------------------------------------


def _delta(workdir: Path, turns: int) -> dict[str, Any]:
    store = SQLiteStore(workdir / "delta.db")
    for i in range(6):
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", src="tool_result", pipeline_id="p", written_by=f"w{i}",
            content=f"payment retry fact {i}: " + "backoff jitter detail " * 20,
        ))
    handlers = make_handlers(store)

    def run(delta: bool) -> int:
        total = 0
        for _ in range(turns):
            ctx = _call(handlers, "ncp_get_context", {
                "agent_id": "alice", "role": "build", "task": "payment_retry", "slot": "retry",
                "intent": "fix", "pipeline_id": "p", "session_id": f"s_delta_{delta}",
                "compact": False, "delta": delta,
            })["context"]
            total += len(ctx.split("[NCP:SUBCONSCIOUS]", 1)[1].split("[NCP:BUDGET]", 1)[0])
        return total

    before, after = run(False), run(True)
    return {"turns": turns, "chunks": 6, "before": before, "after": after,
            "note": "chunk-section characters summed over the run; before = delta:false, after = delta:true"}


# --- e. prefix stability ---------------------------------------------------------------


def _prefix_stability(workdir: Path) -> dict[str, Any]:
    def build(show_score: bool) -> tuple[float, int]:
        store = SQLiteStore(workdir / f"prefix_{int(show_score)}.db")
        for i in range(4):
            store.write(SubconsciousChunk(
                chunk_id=f"c{i}", layer="semantic", src="tool_result", pipeline_id="p", written_by="w",
                content=f"payment retry fact {i}: " + "backoff jitter detail " * 10,
            ))
        store.emit_whisper(Whisper(
            from_agent="planner", target="executor", whisper_type="share", payload={"ask": "check backoff"},
            confidence=0.9, pipeline_id="p", ttl_seconds=7 * 86400,
        ))
        assembler = Assembler(store=store, encoder=PidginEncoder(show_score=show_score))
        conscious = ConsciousBlock(agent_id="executor", role="build", owns=["impl"], must_not=["deploy"],
                                   task="payment_retry", slot="retry", intent="fix", pipeline_id="p")
        contexts = []
        for steps, hours in enumerate((0, 0.05, 1, 6, 30)):
            with patch("time.time", lambda h=hours: _REAL_TIME() + h * 3600):
                contexts.append(assembler.assemble(
                    conscious=conscious, budget=BudgetContext(steps_completed=steps), query_text="payment retry",
                ).context)
        common = len(os.path.commonprefix(contexts))
        mean_len = sum(len(c) for c in contexts) / len(contexts)
        return round(common / mean_len, 4), common

    before_frac, before_chars = build(True)
    after_frac, after_chars = build(False)
    return {
        "turns": 5, "simulated_span_hours": 30,
        "before": before_frac, "after": after_frac,
        "before_stable_chars": before_chars, "after_stable_chars": after_chars,
        "note": "fraction of the block that is byte-identical across turns; before = show_score=true; HIGHER is better",
    }


# --- f. budget accuracy ----------------------------------------------------------------

_PROSE = "The payment retry loop backs off exponentially with jitter and caps the delay at sixty seconds. "
_CODE = "def retry(fn, n=5):\n    for i in range(n):\n        try:\n            return fn()\n        except (IOError, ValueError) as e:\n            sleep(min(60, 2**i))\n"
_JSON = json.dumps({"retry": {"max": 5, "backoff": [1, 2, 4, 8, 16], "jitter": True, "codes": [429, 500, 503]}})


def _budget(workdir: Path, budgets: tuple[int, ...]) -> dict[str, Any]:
    store = SQLiteStore(workdir / "budget.db")
    bodies = [_PROSE * 8, _CODE * 4, _JSON * 6]
    for i in range(18):
        store.write(SubconsciousChunk(
            chunk_id=f"m{i}", layer="semantic", src="tool_result", pipeline_id="p", written_by=f"w{i}",
            content=f"retry note {i} payment backoff: " + bodies[i % 3],
        ))
    assembler = Assembler(store=store)
    conscious = ConsciousBlock(agent_id="executor", role="build", owns=["impl"], must_not=["deploy"],
                               task="payment_retry", slot="retry", intent="fix", pipeline_id="p")
    rows = []
    for max_tokens in budgets:
        ctx = assembler.assemble(
            conscious=conscious, budget=BudgetContext(), query_text="payment retry backoff",
            k=18, max_tokens=max_tokens,
        ).context
        conservative = estimate_budget_tokens(ctx)
        rows.append({
            "max_tokens": max_tokens, "conservative_tokens": conservative,
            "plain_chars_div4_tokens": estimate_tokens(ctx),
            "ratio": round(conservative / max_tokens, 4), "within_budget": conservative <= max_tokens,
        })
    return {"rows": rows, "max_ratio": max(r["ratio"] for r in rows),
            "never_exceeds": all(r["within_budget"] for r in rows)}


# --- driver ----------------------------------------------------------------------------


def run_harness_overhead_benchmark(
    *,
    workdir: str | Path,
    workers: int = 8,
    skill_chars: int = 6000,
    response_chars: int = 20_000,
    delta_turns: int = 10,
    budgets: tuple[int, ...] = (400, 800, 1600, 3200),
) -> dict[str, Any]:
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    with _token_unit_pinned():
        metrics: dict[str, Any] = {
            "a_tool_catalog_chars": _catalog(root),
            "b_protocol_call_chars_per_turn": _protocol_args(root),
            "c_handoff_return_chars": _handoff_return(root, response_chars),
            "d_resent_chunk_chars": _delta(root, delta_turns),
            "e_prefix_stable_fraction": _prefix_stability(root),
            "f_budget_accuracy": _budget(root, budgets),
            "g_dispatch_chars": _skill_dispatch(root, workers, skill_chars),
        }
        unit = token_unit()

    chars = metrics["a_tool_catalog_chars"]["chars"]
    lower_is_better = ["a_tool_catalog_chars", "b_protocol_call_chars_per_turn", "c_handoff_return_chars",
                       "d_resent_chunk_chars", "g_dispatch_chars"]
    checks = {f"{name}_after_le_before": metrics[name]["after"] <= metrics[name]["before"] for name in lower_is_better}
    checks["e_prefix_stable_after_ge_before"] = (
        metrics["e_prefix_stable_fraction"]["after"] >= metrics["e_prefix_stable_fraction"]["before"]
    )
    checks["f_never_exceeds_budget"] = metrics["f_budget_accuracy"]["never_exceeds"]
    checks["a_catalog_sizes_strictly_increase"] = (
        chars["minimal"] < chars["core"] < chars["standard"] < chars["full"]
    )
    checks["b_both_shapes_accepted"] = metrics["b_protocol_call_chars_per_turn"]["both_accepted"]
    return {
        "benchmark": "harness_overhead",
        "unit": unit,
        "provider": "mock (no provider calls, no network)",
        "metrics": metrics,
        "summary": {"pass": all(checks.values()), "checks": checks},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8, help="Workers dispatched with the same skill.")
    parser.add_argument("--skill-chars", type=int, default=6000, help="Size of the workflow skill.")
    parser.add_argument("--response-chars", type=int, default=20_000, help="Provider response size for handoffs.")
    parser.add_argument("--delta-turns", type=int, default=10, help="Turns in the delta run.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ncp-bench-overhead-") as tmpdir:
        artifact = run_harness_overhead_benchmark(
            workdir=tmpdir, workers=args.workers, skill_chars=args.skill_chars,
            response_chars=args.response_chars, delta_turns=args.delta_turns,
        )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
