# README Graph and Memory Positioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the README present NCP 1.4.0 as an agent-to-agent protocol backed by a graph-native, trust-aware memory layer.

**Architecture:** Apply one targeted documentation pass to `README.md`. Strengthen the top-level product hierarchy and examples, reuse links to the existing detailed graph and semantic-memory sections, and correct stale computed-drift wording without changing runtime behavior.

**Tech Stack:** Markdown, Mermaid, Click CLI help, Git.

## Global Constraints

- Preserve the claim that NCP is a substrate rather than an orchestrator.
- Describe only behavior shipped in NCP 1.4.0.
- Keep the existing detailed graph and semantic-memory sections; avoid duplicating their full contents near the top.
- Do not change runtime behavior or public APIs.
- Do not rework the benchmark narrative or redesign the complete README.

---

### Task 1: Reposition Graph-Native Memory in the README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: shipped CLI commands `ncp memory remember`, `ncp memory recall`, `ncp memory improve`, and `ncp graph`.
- Produces: a README whose opening, problem table, quickstart, architecture diagram, and capability overview accurately represent NCP 1.4.0.

- [ ] **Step 1: Capture the current command contracts**

Run:

```bash
python3 -m ncp.cli memory --help
python3 -m ncp.cli memory remember --help
python3 -m ncp.cli memory recall --help
python3 -m ncp.cli graph --help
```

Expected: all commands exit successfully and confirm the option names used in
the README examples.

- [ ] **Step 2: Strengthen the opening and problem table**

Edit the opening to describe NCP as:

```text
An agent-to-agent communication protocol over MCP, backed by a graph-native,
trust-aware memory layer.
```

Add concise problem/solution rows for flat memory and unstructured recall.
Retain the explicit distinction between the bus and the orchestrator.

- [ ] **Step 3: Add a minimal memory-and-graph quickstart**

After the install/server commands, add a short runnable sequence using:

```bash
echo "Alice owns release verification." \
  | ncp memory remember --stdin --pipeline-id demo
ncp memory recall "who owns release verification?" --pipeline-id demo
ncp graph --pipeline-id demo --format json
```

Keep provider-specific setup immediately after this core quickstart.

- [ ] **Step 4: Update architecture and primary capability hierarchy**

Revise the Mermaid architecture diagram to show:

```text
MCP hosts -> NCP runtime -> semantic compiler / graph-aware retrieval
          -> bounded context assembler -> SQLite / pgvector / Redis
```

Add a compact “Graph-native memory layer” overview near the architecture that
summarizes semantic compilation, typed edges, bounded multi-hop retrieval,
bi-temporal views, consolidation, trust calibration, and outcome-credit
propagation. Link to the existing detailed “Memory graph” and “Semantic memory
layer” sections instead of repeating their implementation details.

- [ ] **Step 5: Correct computed-drift wording**

Replace the stale claim that runtime-computed drift is future work. State that:

```text
drift_score remains a client-provided advisory input by default; when
[drift].drift_computed_enabled is enabled, NCP computes a runtime drift score
from recent turn history and uses it for context assembly.
```

Verify the exact configuration name against `ncp/config.py`.

- [ ] **Step 6: Verify claims, commands, links, and formatting**

Run:

```bash
python3 -m ncp.cli memory --help
python3 -m ncp.cli memory remember --help
python3 -m ncp.cli memory recall --help
python3 -m ncp.cli graph --help
python3 -m pytest -q tests/test_cli.py tests/test_memory_layer.py tests/test_graph_cli.py
git diff --check
```

Check every newly referenced relative Markdown link resolves to a repository
file or README anchor. Expected: CLI help exits `0`, focused tests pass, and
`git diff --check` reports no errors.

- [ ] **Step 7: Review the final README diff**

Run:

```bash
git diff -- README.md
```

Confirm the diff is limited to the approved product-positioning scope and
still states that NCP does not schedule agents or replace an orchestrator.

- [ ] **Step 8: Commit**

```bash
git add README.md
git commit -m "docs: surface graph-native memory in README"
```
