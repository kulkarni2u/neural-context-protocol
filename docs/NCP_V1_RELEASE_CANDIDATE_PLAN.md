# NCP V1 Release Candidate Plan

This document defines the work required to turn the current NCP repository into
a coherent V1 release candidate.

The goal is not to add another major capability line. The goal is to:

- stabilize the product story
- remove orchestrator-specific framing from NCP public docs
- make the release surface coherent
- define a clear stop line for calling NCP a complete V1 product

---

## Product Goal

NCP V1 should be presented as:

- a local-first context runtime for multi-agent systems
- a shared MCP-accessible memory and retrieval layer
- a bounded-context assembly engine
- a durable memory store with a simple local mode and a scalable mode

NCP should **not** be presented as:

- an internal orchestration feature
- a Claude/OpenCode-specific workflow wrapper
- a generic AI orchestration product

Those integrations are valid proof points, but they are not the product
definition.

---

## Current State

What is already real in the repo:

- SQLite-first local runtime
- HTTP/SSE MCP transport
- shared memory, retrieval, whispers, and fetch flows
- pgvector durable backend
- Redis-backed ephemeral coordination
- async pgvector path
- bounded-context benchmarks
- MACE benchmark
- operator tooling:
  - `ncp status`
  - `ncp cost`
  - `ncp explain`
  - `ncp viz`
  - `ncp batch`
  - `ncp consolidate`
  - `ncp calibrate`
- extensive test coverage

What is still inconsistent:

- versioning and roadmap language are not fully coherent across docs
- release-candidate acceptance criteria are not written down in one place
- some historical docs still describe milestone progress in a way that is more
  useful for maintainers than for first-time readers

---

## V1 Stop Line

NCP should be marked as a complete V1 release candidate when all of the
following are true:

1. The public product definition is stable.
   - NCP is described as a context runtime and shared memory layer.
   - Public docs do not depend on any internal orchestration naming.

2. The runtime modes are explicit and documented.
   - local/default mode: SQLite
   - scalable mode: pgvector + Redis
   - first-run setup makes that choice explicit

3. The retrieval contract is stable enough to stop active architectural churn.
   - current `0.16.x` retrieval cleanup is closed
   - known retrieval limitations are documented rather than actively reshaped

4. The public documentation is coherent.
   - README, setup docs, protocol docs, benchmark docs, and roadmap docs tell
     the same product story

5. The release story is coherent.
   - version line, changelog, and package metadata match the intended V1 RC

6. The repo demonstrates trustworthiness.
   - tests green
   - benchmark artifacts reproducible
   - MCP path documented and proven

This is the stop line for V1. Everything beyond that is V2+ or post-RC
hardening.

---

## Out of Scope For V1 RC

The following should not block the release candidate:

- full feature parity between SQLite and pgvector on every advanced retrieval mode
- new provider integrations
- new orchestration surfaces
- hosted/cloud control plane
- more benchmark families beyond current benchmark coverage
- UI/dashboard productization outside the current CLI/MCP/operator surface

---

## Workstreams

### 1. Product Positioning Cleanup

Objective:
- make NCP read like a standalone product, not a sidecar to an internal orchestration process

Required changes:
- remove orchestration-specific framing from public docs
- rewrite public wording that implies NCP depends on a specific orchestrator

Primary files:
- `README.md`
- `docs/NCP_SETUP.md`
- `docs/NCP_MCP_DOGFOOD_LOOP.md`
- `docs/NCP_ACTIVE_HANDOFF_PACKET.md`
- `CHANGELOG.md`

Acceptance criteria:
- a new reader can understand NCP without knowing any internal process exists
- any retained integration examples read as optional host usage, not product identity

### 2. Release Story Cleanup

Objective:
- make the version, milestone, and roadmap story internally consistent

Required changes:
- normalize V1 RC language across:
  - package version
  - changelog headings
  - roadmap docs
  - README
- decide whether to keep the internal `0.16.x` line only as changelog history
  while presenting the product as a single V1 RC surface

Primary files:
- `pyproject.toml`
- `ncp/version.py`
- `CHANGELOG.md`
- `README.md`
- `docs/NCP_POST_V1_ROADMAP.md`

Acceptance criteria:
- version naming and roadmap naming do not contradict each other
- README no longer calls the product both “early alpha” and effectively
  production-shaped at the same time

### 3. Retrieval Closure

Objective:
- close the current retrieval architecture line enough to stop churn for V1 RC

Required changes:
- finish the active retrieval cleanup
- explicitly document the remaining SQLite vs pgvector differences
- move unresolved retrieval ambitions into post-V1 roadmap

Primary files:
- `ncp/stores/retrieval.py`
- `ncp/stores/sqlite.py`
- `ncp/stores/pgvector.py`
- `ncp/stores/pgvector_async.py`
- `docs/NCP_ACTIVE_HANDOFF_PACKET.md`
- `README.md`

Acceptance criteria:
- no active “one more retrieval refactor” loop remains for V1
- known limitations are documented clearly

### 4. Guided Setup Experience

Objective:
- make first-run setup feel like a product entry point instead of a doc-only path

Required changes:
- extend `ncp init` or add a guided setup path that asks:
  - do you want local/default setup with SQLite?
  - do you want scalable/local-lab setup with pgvector + Redis?
- if SQLite is chosen:
  - write the normal `.ncp/config.toml`
  - keep setup minimal
- if pgvector + Redis is chosen:
  - generate config pointing at the local scalable stack
  - point users to the compose-based local infra path
  - make it clear which extras/dependencies are required
- treat the current compose stack as a first-class local developer/operator path
  instead of an advanced hidden path

Primary files:
- `ncp/cli.py`
- `ncp/templates/config.toml.example`
- `docs/NCP_SETUP.md`
- `README.md`
- `compose.yaml`
- `scripts/infra_up.sh`
- `scripts/infra_down.sh`

Acceptance criteria:
- a first-time user can choose SQLite or pgvector + Redis without manually
  reverse-engineering docs
- the scalable path is runnable locally via compose with one obvious setup flow
- README and setup docs describe both modes consistently
### 5. README Overhaul

Objective:
- make README work as the primary project landing page and release-candidate page

Required changes:
- stronger “what NCP is” opening
- clearer architecture explanation
- concise proof points
- install/setup path
- local mode vs scalable mode
- benchmark summary
- operator workflow summary
- integration examples moved lower in the document

Recommended additions:
- one architecture diagram
- one context-flow diagram
- one quick operator workflow diagram

Acceptance criteria:
- README answers:
  - what is NCP
  - why it exists
  - how it works
  - how to run it
  - what is proven
  - what modes exist

### 6. Documentation Sweep

Objective:
- align the rest of the docs to the V1 RC positioning

Required changes:
- classify docs into:
  - normative/public
  - roadmap/internal-ish but public
  - benchmark evidence
- tighten wording and remove stale milestone framing

Primary targets:
- `docs/NCP_SETUP.md`
- `docs/NCP_PROTOCOL_SPEC.md`
- `docs/NCP_MCP_DOGFOOD_LOOP.md`
- `docs/NCP_BENCHMARK_CODING_PIPELINE.md`
- `docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md`
- `benchmarks/mace/README.md`
- `docs/NCP_POST_V1_ROADMAP.md`

Acceptance criteria:
- no public doc implies NCP is primarily part of an internal orchestration stack
- no public doc contradicts the README’s product definition

### 7. Visual Support

Objective:
- use diagrams/images to make the README easier to understand quickly

Planned visual assets:
- architecture diagram
  - clients -> MCP -> runtime -> store modes
- retrieval/assembly flow diagram
  - conscious block -> retrieval -> whispers -> assembled context
- mode comparison diagram
  - SQLite mode vs pgvector + Redis mode

Preferred implementation:
- Mermaid diagrams in README/docs first
- optional static images later if needed

Acceptance criteria:
- diagrams clarify architecture without adding marketing fluff

---

## Proposed Release Sequence

### Release Slice A — Positioning + README

- rewrite README
- remove orchestration-centric framing from core public docs
- add diagrams

### Release Slice B — Guided Setup

- add SQLite vs pgvector + Redis guided setup choice
- make the compose-based local scalable path first-class

### Release Slice C — Version + Changelog Coherence

- normalize V1 RC language
- clean roadmap/release naming

### Release Slice D — Retrieval Closure

- finish the current retrieval cleanup
- document remaining backend differences explicitly

### Release Slice E — Final RC Verification

- full test suite
- benchmark artifact sanity check
- MCP setup doc verification
- package metadata/doc cross-check

### Release Slice F — V1 RC Publish

- cut the release candidate version
- update changelog
- publish package/release

---

## Exact Docs Cleanup Inventory

### Must update

- `README.md`
- `CHANGELOG.md`
- `docs/NCP_SETUP.md`
- `ncp/cli.py`
- `docs/NCP_MCP_DOGFOOD_LOOP.md`
- `docs/NCP_ACTIVE_HANDOFF_PACKET.md`
- `docs/NCP_POST_V1_ROADMAP.md`

### Should review

- `docs/NCP_PROTOCOL_SPEC.md`
- `docs/NCP_BENCHMARK_CODING_PIPELINE.md`
- `docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md`
- `benchmarks/mace/README.md`

### Specific orchestration-related cleanup

Current public references that should be reframed or reduced:

- README proof bullets that mention internal orchestration examples
- README token-efficiency examples labeled like internal handoff flows
- `docs/NCP_MCP_DOGFOOD_LOOP.md` sections that describe internal orchestration
  usage as if it defines NCP’s intended product posture
- `docs/NCP_ACTIVE_HANDOFF_PACKET.md` recommended roles/orchestration loop

Policy:
- keep NCP-first product language
- move orchestration references into “validation / integration example” framing

---

## Recommended Acceptance Criteria For V1 RC

NCP V1 RC is complete when:

- README and primary docs describe NCP without relying on internal orchestration framing
- README and primary docs describe NCP as a plug-and-play MCP connector/runtime
- runtime modes are explicit and coherent
- first-run setup offers a clear SQLite vs pgvector + Redis choice
- current retrieval line is closed for V1
- changelog/version story is coherent
- test suite is green
- benchmark docs are truthful and reproducible
- MCP usage/setup story is understandable to a new user

---

## Immediate Next Action

Recommended next execution step:

1. Close the remaining retrieval and version-story cleanup
2. Run the final release-candidate verification sweep
3. Cut the V1 release candidate
