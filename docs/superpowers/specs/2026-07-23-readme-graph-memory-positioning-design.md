# README Graph and Memory Positioning Design

## Goal

Make the README reflect NCP 1.4.0 as an agent-to-agent communication protocol
with a graph-native, trust-aware memory layer, while preserving the boundary
that NCP is a substrate rather than an orchestrator.

## Approach

Use a targeted positioning pass rather than a full rewrite:

1. Strengthen the opening description and problem/solution table.
2. Add graph engineering and semantic memory to the primary architecture
   narrative.
3. Put a minimal `remember` / `recall` / `graph` workflow in the quickstart.
4. Add a concise capability overview near the top that links to the existing
   detailed graph and semantic-memory sections.
5. Correct stale statements about computed drift now that opt-in runtime drift
   computation ships in 1.4.0.

## Product Language

The opening should call NCP:

> An agent-to-agent communication protocol over MCP, backed by a graph-native,
> trust-aware memory layer.

The README may describe NCP as a memory system because it now provides
deterministic semantic compilation, layered durable memory, typed
relationships, bounded multi-hop retrieval, consolidation, bi-temporal views,
trust calibration, and outcome-credit propagation. It must not imply that NCP
schedules agents, owns workflow execution, or replaces an orchestrator.

## Information Hierarchy

- Opening: protocol, memory layer, and user outcome.
- Problem table: include flat-memory and unstructured-recall problems.
- Quickstart: keep installation first, then show the memory and graph commands.
- Architecture: include the semantic compiler, graph-aware retrieval, stores,
  and bounded context assembly.
- Capability overview: summarize graph-native memory and link to the detailed
  sections already present later in the README.
- Detailed sections: retain their technical depth and avoid duplicating them
  near the top.

## Verification

- Check every new command against `ncp --help` and the relevant subcommand help.
- Check every capability claim against the 1.4.0 implementation and tests.
- Ensure all relative README links resolve.
- Run the repository's README or documentation tests when available; otherwise
  run the focused CLI/help checks plus `git diff --check`.
- Confirm the edit does not change the claim that NCP is not an orchestrator.

## Out of Scope

- Changing runtime behavior or public APIs.
- Reworking the benchmark narrative.
- Redesigning the complete README.
- Introducing claims beyond behavior shipped in NCP 1.4.0.
