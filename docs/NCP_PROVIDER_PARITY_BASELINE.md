# NCP Provider Parity Baseline
## Live MCP dogfood parity snapshot for Claude, Codex, and OpenCode

This document records the current parity baseline on the shared MCP dogfood
path.

It is not a marketing claim.
It is the current engineering truth for the live CLI-backed continuation path.

## Harness version

- transport: real stdio MCP via `ncp serve`
- scenario: continuation adapter repeatability mode
- attempts per provider: `5`
- per-call timeout: provider-specific, recorded below

Commands used:

```bash
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter claude-cli --attempts 5 --adapter-timeout-seconds 20
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter codex-cli --attempts 5 --adapter-timeout-seconds 20
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter opencode-cli --attempts 5 --adapter-timeout-seconds 18
```

## Current results

| Provider | Attempts | Contract success | Continuation success | Stable | Working timeout |
|---|---:|---:|---:|---|---:|
| `claude-cli` | 5 | 5/5 | 5/5 | Yes | `20s` |
| `codex-cli` | 5 | 5/5 | 5/5 | Yes | `20s` |
| `opencode-cli` | 5 | 5/5 | 5/5 | Yes | `18s` |

## Interpretation

### Claude

- Claude follows the strict continuation contract reliably on the current
  harness shape.
- The working baseline for this slice is the tightened Claude-specific prompt
  plus a `20s` per-call timeout.

### OpenCode

- OpenCode returned contract-shaped and semantically correct output on all 5
  attempts in this baseline.
- The working baseline for this slice is the current JSON contract path plus an
  `18s` per-call timeout.

### Codex

- Codex follows the strict continuation contract reliably on the current
  harness shape.
- The working baseline for this slice is the `codex exec` non-interactive path
  plus a `20s` per-call timeout.

## What this means for parity

At the moment:

- all three providers are operational on the live MCP dogfood path
- all three providers pass this bounded parity slice cleanly
- the parity claim is limited to this exact continuation scenario and these
  explicit timeout budgets

More precisely:

- Claude, Codex, and OpenCode all pass the bounded `ncp_fetch` continuation
  slice on the live MCP dogfood path

## Current claim

The honest claim today is:

- Claude, Codex, and OpenCode all support the bounded `ncp_fetch`
  continuation pattern on the live MCP dogfood path
- current baseline is `5/5` contract success and `5/5` continuation success
  for all three providers on the current harness version
