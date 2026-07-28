# Provider Context-Artifact Audit

This audit is a release gate for model-facing NCP context artifacts. It is not
a runtime dependency. Deterministic results always keep Claude, Codex, and
OpenCode separate; there is no combined "agent" average.

## Deterministic audit

Run the current inventory and the checked-in right-sizing candidate:

```bash
python3 benchmarks/context_artifacts/run.py \
  --repo-root . \
  --candidate rightsized-v1 \
  --output /tmp/ncp-context-artifacts.json
```

The inventory imports `CLAUDE_MD_TEMPLATE` from Python, reads shell and
JavaScript sources without executing them, includes checked-in example mirrors,
and counts the shared MCP tool metadata exactly once for each provider. Token
counts use the unit reported in `token_unit`. Candidate comparisons report
current and candidate metrics plus per-provider deltas. Every candidate remains
`live_evaluation_required`: fewer tokens alone never establish that it is
better.

The `rightsized-v1` candidate contains model-facing text only. It keeps the
complete retrieved-content trust boundary and core lifecycle coverage, removes
repeated lifecycle tutorials, and leaves endpoint liveness in each provider's
`session-context.txt`. It does not copy executable hooks.

## Opt-in live evaluation

Live calls are excluded from CI. Run each provider explicitly; the harness
never substitutes one provider for another:

```bash
python3 benchmarks/context_artifacts/run.py --live --provider claude-cli --condition current --seeds 3
python3 benchmarks/context_artifacts/run.py --live --provider codex-cli --condition current --seeds 3
python3 benchmarks/context_artifacts/run.py --live --provider opencode-cli --condition current --seeds 3
```

Use `--condition rightsized-v1` to evaluate the candidate. The harness uses the
existing CLI adapters from `ncp.dogfood`. Each seed is an explicit repeat ID in
the prompt; provider CLIs retain control of their own sampling configuration.
The three fixed scenarios are:

1. a normal bounded-context turn;
2. a retrieved low-trust chunk containing a malicious instruction;
3. a subagent handoff requiring pre-context and post-memory lifecycle.

Standard output is JSONL with one attempt per provider, condition, seed, and
scenario. Every attempt includes `provider`, `model`, `condition`, `seed`,
`prompt_tokens`, `lifecycle_order_compliance`,
`trust_boundary_compliance`, `task_success`, `timeout`, and
`raw_artifact_ref`. Compliance fields are derived from required response
markers in the saved raw provider response; they remain `null` on skips,
timeouts, and errors.

Raw prompts and responses default to `/tmp/ncp-context-artifact-live`; override
that with `--raw-dir`. If the requested provider CLI is unavailable, the
harness emits a structured `provider_unavailable` skip for every seed and
scenario and makes no live call.

`TEMPLATE.json` documents the JSONL shape. Its placeholders and `null` values
are a schema example, not claimed live results.
