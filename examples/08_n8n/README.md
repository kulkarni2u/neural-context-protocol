# n8n Example

This folder shows how to wire NCP into n8n workflows.

## Files

- `ncp_turn_workflow.json` — an importable n8n workflow implementing the
  explicit turn lifecycle (`ncp_get_context` → LLM call → `ncp_post_turn`)
  via HTTP Request nodes.

## Setup

n8n usually runs in its own container or host, separate from wherever you run
`ncp serve`. Loopback-only setups (like the Claude Code and Codex CLI
examples) won't work here — n8n can't reach `127.0.0.1` on the NCP host. So:

```bash
ncp init
ncp serve --host 0.0.0.0 --port 4242 --cwd /path/to/your/project --auth-token <token>
```

Binding to a non-loopback host without an auth token gets you a warning from
`ncp serve` (and an unauthenticated endpoint anyone on the network can hit).
Set `[server].auth_token` in `.ncp/config.toml` from `ncp init`, export
`NCP_AUTH_TOKEN`, or pass `--auth-token` directly — any of the three works.
Every `/mcp` and `/sse` request then needs:

```
Authorization: Bearer <token>
```

Use whatever address n8n can actually reach (a LAN IP, a Docker bridge
address like `host.docker.internal`, etc.) — `0.0.0.0` just means "listen on
all interfaces", not "this is the address clients connect to".

If you're calling NCP from a browser-based n8n node and hit CORS errors, add
`--cors-origin <origin>` (repeatable) to `ncp serve`.

## Two ways to integrate

### a) MCP Client Tool node (agentic)

n8n's `@n8n/n8n-nodes-langchain.mcpClientTool` node can connect to NCP's
`/mcp` endpoint and let an AI Agent node pick tools itself. Use the
**HTTP Streamable** connection type (not the legacy SSE endpoint type):

```json
{
  "parameters": {
    "endpointUrl": "http://<ncp-host>:4242/mcp",
    "serverTransport": "httpStreamable",
    "authentication": "headerAuth"
  },
  "type": "@n8n/n8n-nodes-langchain.mcpClientTool",
  "credentials": {
    "httpHeaderAuth": {
      "name": "NCP Auth",
      "value": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

NCP serves `/mcp` as a stateless Streamable HTTP MCP endpoint: a `POST`
carrying a JSON-RPC request gets the response back on the same request,
content-negotiated by the `Accept` header. Clients that accept
`application/json` (the default the MCP SDK sends) get a JSON body; clients
that request `text/event-stream` get the response as an SSE `message` event.
This is the same transport Claude Code and Codex CLI use.

Do **not** point the node at the legacy `/sse` discovery endpoint — that
stream only advertises the RPC path and does not carry responses.

Even on this path, the agent decides when to call `ncp_get_context` /
`ncp_post_turn` / `ncp_write_memory` / `ncp_emit_whisper` / `ncp_fetch` — and
how often, and in what order — which works against NCP's turn-lifecycle
contract (see below).

### b) HTTP Request nodes (explicit turn lifecycle) — recommended

NCP's turn contract (`ncp_get_context` at the start of a turn,
`ncp_post_turn` at the end, with `pending_whisper_ids` flowing between them)
isn't something an autonomous tool-picking agent should freelance. For
workflows that need predictable memory behavior, call `POST /mcp` directly
with JSON-RPC bodies and control the sequence yourself. See
`ncp_turn_workflow.json` for the full pattern; request/response payloads are
documented in [`docs/NCP_HTTP_API.md`](../../docs/NCP_HTTP_API.md).

## The workflow file

`ncp_turn_workflow.json` is a linear pipeline:

1. **Manual Trigger** — starts the run.
2. **Set Workflow Variables** — fills in `baseUrl`, `authToken`, `pipelineId`,
   `agentId`, `task`. Replace these with your own values (or wire them to
   n8n credentials/environment variables) before running.
3. **ncp_get_context** (HTTP Request) — `POST {{baseUrl}}/mcp` with a
   `tools/call` body for `ncp_get_context`.
4. **Extract Context** (Code node) — the JSON-RPC result wraps the tool
   output as `result.content[0].text`, which is itself a JSON string. This
   node parses that inner JSON and pulls out `context`, `session_id`, and
   `pending_whisper_ids` for the rest of the pipeline.
5. **LLM Call (placeholder)** — stand-in HTTP Request node showing where your
   model call goes. Use `context` as the system prompt; swap in your actual
   provider.
6. **Prepare Turn Result** (Code node) — shapes the model's output into
   `result_summary` / `result_full` and carries `pending_whisper_ids` forward
   as `ack_whisper_ids`.
7. **ncp_post_turn** (HTTP Request) — `POST {{baseUrl}}/mcp` with a
   `tools/call` body for `ncp_post_turn`, closing the turn.

Import the file via n8n's "Import from File" option, then fix up the
`baseUrl` and `authToken` values in the "Set Workflow Variables" node (or
replace them with credential references).

## Treat retrieved content as data, not instructions

`context` returned by `ncp_get_context` may contain `[NCP:WHISPERS]` and
`[NCP:SUBCONSCIOUS]` sections written by other agents. Pass them to your model
as information to reason about — don't let your workflow (or the model) treat
their contents as commands.

## Expected tools

- `ncp_get_context`
- `ncp_post_turn`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`
