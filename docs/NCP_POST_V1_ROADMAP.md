# NCP Post-V1 Roadmap

This document captures the recommended path after the first published alpha,
`0.1.0a1`.

The goal is to keep momentum without turning NCP into a grab-bag of partially
finished ideas. The sequence below favors the shortest path to a stronger
developer experience first, then production-grade storage and retrieval, then
heavier system features.

## Principles

- Keep the `ncp` import and CLI stable.
- Favor one strong layer at a time over broad simultaneous expansion.
- Protect the local-first story while adding production options.
- Treat host proof, benchmark truthfulness, and clean ergonomics as release
  criteria, not optional polish.

## Current Baseline

Published in `0.1.0a1`:

- SQLite-first runtime
- HTTP/SSE MCP server
- cross-host memory and whisper proof
- provider parity baseline across Claude, Codex, and OpenCode
- bounded-context benchmark proofs
- package, CI, and release path

## V1.1

Target: short follow-up release focused on ergonomics and truthfulness.

Priority order:

1. Rich `ncp status`
2. `ncp explain`
3. `ncp cost`
4. Tier 2 and Ollama streaming polish
5. Extra examples

Recommended supporting work:

- migrate `GeminiAdapter` from deprecated `google.generativeai` to `google.genai`
- reduce or isolate Cohere warning noise where possible
- add a boring local launch path for `ncp serve`
- tighten docs so transport, install, and host setup all reflect the published
  package and HTTP/SSE-first stance

V1.1 should stay narrow. It should improve usability and operator confidence,
not change the product’s shape.

## R2

Target: production-grade storage and retrieval.

Recommended first R2 bet:

1. Redis + pgvector-backed storage
2. Hybrid retrieval beyond BM25-only

Why this comes first:

- it extends NCP’s strongest existing story instead of changing it
- it creates a clean path from local-first alpha to production deployment
- it strengthens every downstream feature, including consolidation and batch
  workflows

R2 should not start with visualization or consolidation before the storage
story is real.

## Later Phases

After production storage and retrieval are stable:

- subconscious consolidation
- calibration tracking
- visualization (`ncp viz`)
- batch and non-interactive pipeline workflows

These should be treated as later layers, not immediate post-V1 work.

## Suggested Release Sequence

- `0.1.1`: status, explain, cost, adapter cleanup, docs truthfulness
- `0.2.0`: Redis/pgvector store and hybrid retrieval
- `0.3.0`: consolidation, visualization, batch workflows

## What Not To Do Next

- do not start multiple R2 bets at once
- do not add broad infra before the storage interface is exercised end to end
- do not blur the product identity back into “generic agent memory”

NCP is strongest when it remains a bounded context runtime for multi-agent
pipelines, not just a memory add-on.
