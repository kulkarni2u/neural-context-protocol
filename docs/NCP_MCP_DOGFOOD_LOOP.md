# NCP MCP Dogfood Loop
## Deterministic MCP proof for NCP V1

This document describes the deterministic dogfood loops that now ship inside
the package.

It is intentionally narrower than the full multi-provider Sarathi run.
The purpose is to prove the transport and persistence contract before we start
claiming real provider parity.

## What landed

The package now includes:

- `ncp.dogfood.MCPHTTPClient`
- `ncp.dogfood.MCPStdioClient`
- `ncp.run_canonical_http_dogfood_loop(...)`
- `ncp.run_canonical_dogfood_loop(...)`
- `ncp.run_adapter_continuation_dogfood_loop(...)`
- `ncp.run_repeatability_dogfood_loop(...)`
- `ncp dogfood`

There are now two validation paths:

- public HTTP/SSE validation against `ncp serve`
- internal stdio compatibility validation against `ncp serve-stdio`

Neither path is an in-process shortcut.

## Default role map

The dogfood artifact defaults to the working NCP role split:

- planner: `claude`
- executor: `opencode`
- critic: `codex`

These are labels in the first deterministic proof artifact.
They define the intended Sarathi routing posture without pretending that the
real provider turn loop is already complete.

## What the loop proves

The default `ncp dogfood` path proves these things end to end:

1. `initialize` and `tools/list` work over the public HTTP/SSE transport.
2. `ncp_write_memory` writes durable chunks into the SQLite store.
3. `ncp_get_context` returns assembled context and a fetch session token.
4. the host triggers one `ncp_fetch` and reinjects the result into the same turn.
5. `ncp_fetch` works in the same host session and returns the persisted chunk.
6. Restarting the MCP server does not lose the stored memory.

That is enough to validate the public transport path used by hosts.

The hidden `serve-stdio` compatibility path still exists for internal tests and
lower-level dogfood coverage.

## Adapter continuation mode

There is now a second bounded proof mode:

- adapter call 1 must return `NCP_FETCH_REQUEST`
- host executes one `ncp_fetch`
- adapter call 2 must return `NCP_FINAL`

The first shipped proof adapter is:

- `DogfoodLocalAdapter`

This is a deterministic adapter that follows the contract exactly so the host
logic can be verified without depending on external API availability.

The same path can be attempted later with:

- `claude-cli`
- `codex-cli`
- `opencode-cli`
- `anthropic`
- `openai`
- `ollama`
- `gemini`
- `mistral`
- `cohere`

External adapters are optional here and depend on local credentials and model
behavior.

If credentials are missing, the harness must report that explicitly.
It must not imply that a live provider run succeeded or was even attempted.

## Repeatability mode

There is now a compact repeatability mode for continuation adapters:

- `ncp dogfood --continuation-adapter opencode-cli --attempts 5`
- `ncp dogfood --continuation-adapter claude-cli --attempts 5 --adapter-timeout-seconds 20`
- `ncp dogfood --continuation-adapter codex-cli --attempts 5 --adapter-timeout-seconds 20`

This mode exists to support Sarathi provider stabilization work without
attaching oversized raw artifacts to every task event.

The repeatability artifact includes:

- requested attempts
- per-attempt status
- per-attempt timeout/runtime error details when present
- success rate
- continuation success rate
- a boolean `summary.stable` gate

## Current observed state

Observed on May 24, 2026:

- `opencode-cli`
  - earlier runs were mixed, including one success, one timeout, and one
    non-contract miss payload
  - current baseline is a 5-attempt repeatability pass with `5 success / 0 error`
    and `5/5` continuation success at `18s` per call
  - current interpretation: dependable on the current bounded prompt shape and timeout budget

- `claude-cli`
  - strict fetch-request probing worked
  - earlier runs failed due to timeout pressure and contract drift
  - after tightening the Claude-specific prompts and moving to a `20s` per-call
    budget, the current 5-attempt repeatability pass completed with
    `5 success / 0 error` and `5/5` continuation success
  - current interpretation: dependable on the current bounded prompt shape and timeout budget

- `codex-cli`
  - the `codex exec` non-interactive path now has a matching continuation adapter
  - the current 5-attempt repeatability pass completed with `5 success / 0 error`
    and `5/5` continuation success at `20s` per call
  - current interpretation: dependable on the current bounded prompt shape and timeout budget

## What it does not prove yet

It does not yet prove:

- real external-model continuation after `ncp_fetch`
- Sarathi evidence capture from a live provider run
- provider parity beyond the current bounded continuation slice

Those are the next layer, not this layer.

The current live parity snapshot is recorded in:

- `docs/NCP_PROVIDER_PARITY_BASELINE.md`

## How to run

From the repo:

```bash
ncp dogfood --cwd /path/to/project
ncp dogfood --cwd /path/to/project --continuation-adapter local
ncp dogfood --cwd /path/to/project --continuation-adapter codex-cli
ncp dogfood --cwd /path/to/project --continuation-adapter opencode-cli
ncp dogfood --cwd /path/to/project --continuation-adapter claude-cli
ncp dogfood --cwd /path/to/project --continuation-adapter anthropic
ncp dogfood --cwd /path/to/project --continuation-adapter opencode-cli --attempts 5
```

Or directly through Python:

```python
from pathlib import Path
from ncp.dogfood import run_canonical_http_dogfood_loop

artifact = run_canonical_http_dogfood_loop(
    store_path=Path(".ncp/store.db"),
)
```

## Output contract

The command prints one JSON artifact.

Important fields:

- `transport`
- `pipeline_id`
- `provider_roles`
- `tools`
- `first_pass`
- `restart_pass`
- `store_status`
- `restart_persistence_ok`
- `summary`

The key launch signal is:

- `restart_persistence_ok = true`

The key continuation signal is:

- `summary.continuation_ok = true`

In adapter mode, also expect:

- `mode = "adapter_continuation"`
- `adapter = "DogfoodLocalAdapter"` for the deterministic local proof

For external adapters without credentials, expect:

- `mode = "live_adapter_attempt"`
- `status = "missing_credentials"`
- `attempted = false`
- `readiness.credentials_present = false`

For repeatability mode, expect:

- `mode = "repeatability_run"`
- `attempts_detail = [...]`
- `summary.success_rate`
- `summary.continuation_success_rate`
- `summary.stable`

## How Sarathi should use it

Sarathi should treat this loop as the bounded proof step before a real
provider-managed dogfood run.

Recommended order:

1. run `ncp dogfood`
2. record the artifact
3. promote the same topology into a live Sarathi task with:
   - Claude planning
   - OpenCode execution
   - Codex verification
4. use adapter continuation mode with a real external provider
5. only then start provider-parity rotation

That keeps the transport proof and the provider proof separate and honest.
