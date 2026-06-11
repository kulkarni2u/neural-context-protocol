# Your Pipeline Grows. Your Context Shouldn't. — Neural Context Protocol v1.1

**What a single multi-agent session really costs**

Take a 4-agent coding pipeline (analyzer → fixer → reviewer → tester) running 40 turns.

With the default raw-replay approach, the final turn drags **3,426 tokens** of accumulated history into the prompt. With NCP v1.1's bounded context, the same session — with the same task success on the benchmark — uses only **261 tokens** on that turn.

**That's 13.13× fewer tokens at the final turn**, and the gap keeps widening with every turn after that, because raw replay grows linearly while NCP stays flat under a hard budget.

(Honest footnote: this benchmark uses compact synthetic transcripts with a deterministic `chars_div4` token unit and a 340-token benchmark budget, so everyone can reproduce the exact numbers. Real sessions carry 10–20× more tokens per turn — which makes the multiplier matter more, not less.)

Most teams never measure this. The work still finishes… while the token bill quietly compounds.

---

## The Problem Nobody Measures

Every multi-agent pipeline compounds. By turn 30–50 the *useful* working set is still small, but the model re-reads the entire transcript.

The usual fixes each fail differently:

- **Raw replay** — context grows without bound; a 50-turn session can easily reach tens of thousands of tokens per turn
- **Sliding window** — loses early critical facts exactly when you need them
- **Rolling summaries** — quality drift plus extra compute on every turn

**Neural Context Protocol (NCP) v1.1** takes a different approach: a **bounded, persistent, trust-weighted memory bus**.

---

## NCP v1.1: The Memory Bus Your Agents Need

NCP works *under* your orchestrator (LangGraph, CrewAI, AutoGen, Claude Code, Codex CLI, and any other MCP host). Every turn it assembles three compact blocks instead of replaying history:

👉 [INSERT IMAGE: 01_context_budget.png]

The ~840-token figure is the **enforced ceiling**, new in v1.1 — actual usage is often far below it (the benchmark's final turn used just 261). Memory survives restarts and is shared across hosts and agents. v1.1 also adds deterministic token counting, at-least-once whisper delivery, trust-through-MCP, and SQLite FTS5 retrieval.

### How a turn flows

👉 [INSERT IMAGE: 03_turn_flow.png]

### Architecture

👉 [INSERT IMAGE: 04_architecture.png]

---

## 8 Principles for Bounded Multi-Agent Memory

1. Classify memory by source and trust (user_verified, tool_result, …)
2. Never replay full history — assemble bounded context every turn
3. Use whispers for coordination instead of prompt stuffing
4. Track drift explicitly (CoherenceChecker + written_at_drift)
5. Make retrieval relevance- and trust-aware (BM25 + decay)
6. Persist across hosts and restarts via pipeline_id
7. Enforce token budgets at assembly time
8. Measure what matters: tokens, task success at matched budget, MACE score

---

## Benchmarks (Deterministic & Reproducible)

👉 [INSERT IMAGE: 02_benchmarks.png]

Beyond raw token counts:

- MACE multi-agent coordination score: **0.9608**
- Needle recall under a tight budget: **+0.50** over sliding window
- Task success at a matched 400-token budget: **NCP 1.00 vs baseline 0.00**

Every number above is deterministic and reproducible from the repo — run `python3 benchmarks/coding_pipeline/run.py` and you'll get the same figures.

---

## The Scaling Math

A concrete, conservative example. Assume 40-turn sessions where coordination turns replay an average of ~10k tokens of context, priced at $3 per million input tokens (Claude Sonnet class):

- Raw replay: ~400k input tokens per session
- NCP bounded: ~34k input tokens per session
- Savings: roughly **$1.10 per session**

At 1,000 sessions/month that's **~$13k/year**; at 5,000 sessions/month, **~$66k/year** — before counting faster turns, lower latency, and better coherence from a stable working set. Plug in your own session shape; the savings scale linearly.

---

## Get Started (v1.1)

Three commands:

pip install neural-context-protocol
ncp init
ncp serve --host 127.0.0.1 --port 4242

Then try `ncp demo`, the LangGraph example, or the Claude Code setup in `examples/`.

**NCP is MIT-licensed.** Built to make long-running multi-agent systems actually sustainable.

**Repo:** github.com/kulkarni2u/neural-context-protocol
