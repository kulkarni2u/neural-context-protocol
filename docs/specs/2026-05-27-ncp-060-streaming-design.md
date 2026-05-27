# NCP 0.6.x — Slice 1: Streaming `ncp_get_context`

**Date:** 2026-05-27
**Status:** Approved — ready for implementation
**Release target:** 0.6.0

---

## Problem

`ncp_get_context` today returns a single assembled string in one synchronous JSON-RPC
response. On stores with many subconscious chunks, two problems arise:

1. **Latency:** the agent receives zero bytes until full assembly completes.
2. **Size:** large assemblies can hit MCP client buffer limits or connection timeouts.

---

## Goals

- Deliver the `budget_header` and `conscious` sections to the client before
  subconscious chunks are serialized to the wire.
- Eliminate timeout risk by streaming large assemblies in bounded chunks.
- Keep the non-streaming path byte-for-byte identical (zero breaking change).

---

## Approach: `StreamResponse` sentinel

The handler returns either the existing plain `dict` (non-streaming) or a
`StreamResponse` dataclass. The transport layer detects the type and handles
accordingly. Handler logic stays decoupled from transport.

---

## Components changed

### 1. `ncp/assembler.py`

Add one public method:

```python
def apply_post_middleware(self, text: str) -> str:
    return self.middleware.post_assemble(text)
```

Exposes middleware post-processing without making `self.middleware` public.
Allows the server to join streamed sections and apply middleware once, without
calling `assemble()` a second time.

### 2. `ncp/mcp/server.py`

**New `StreamResponse` dataclass** (near top of file, after `FetchSession`):

```python
@dataclass
class StreamResponse:
    sections: list[tuple[str, str]]    # [(label, text), ...] from assemble_incremental
    handler_result: dict[str, object]  # same payload shape as non-streaming return
    request_id: int | str | None = None  # injected by _handle_request
```

**`MCP_TOOLS` schema update** — add to `ncp_get_context` `properties`:

```json
"stream": {
  "type": "boolean",
  "description": "If true, returns sections progressively as NDJSON (HTTP) or JSON-RPC notifications (stdio). Default false."
}
```

**`_handle_get_context` streaming branch** (inside `make_handlers`):

```python
stream = bool(args.get("stream", False))
if stream:
    sections = list(assembler.assemble_incremental(
        conscious=conscious,
        budget=BudgetContext(),
        query_text=conscious.task + " " + conscious.slot,
    ))
    assembled = assembler.apply_post_middleware("\n\n".join(t for _, t in sections))
    return StreamResponse(
        sections=sections,
        handler_result={"context": assembled, "session_id": session_id},
    )
# else: existing assemble() path unchanged
```

**`_handle_request` return type** widens to `str | StreamResponse`:

```python
result = handler(arguments)
if isinstance(result, StreamResponse):
    result.request_id = req_id
    return result
return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
```

**`_MCPHTTPHandler.do_POST`** — add isinstance check before existing response path:

```python
response = _handle_request(payload, self.server.handlers)
if isinstance(response, StreamResponse):
    self._stream_ndjson(response)
elif response:
    self._send_json(HTTPStatus.OK, json.loads(response))
else:
    self._send_empty(HTTPStatus.ACCEPTED)
```

**`_MCPHTTPHandler._stream_ndjson`** — new method:

```python
def _stream_ndjson(self, sr: StreamResponse) -> None:
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "application/x-ndjson")
    self.send_header("Transfer-Encoding", "chunked")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    for i, (label, text) in enumerate(sr.sections):
        line = json.dumps({"type": "ncp_chunk", "section": label, "index": i, "text": text}) + "\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()
    final = _ok(sr.request_id, {"content": [{"type": "text", "text": json.dumps(sr.handler_result)}]})
    self.wfile.write((final + "\n").encode("utf-8"))
    self.wfile.flush()
```

**`serve_streams`** — add isinstance check in the response-dispatch loop:

```python
response = _handle_request(req, handlers)
if isinstance(response, StreamResponse):
    for i, (label, text) in enumerate(response.sections):
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "ncp/stream_chunk",
            "params": {
                "request_id": response.request_id,
                "section": label,
                "index": i,
                "text": text,
            },
        })
        _write_message(output_stream, notif)
    final = _ok(response.request_id, {"content": [{"type": "text", "text": json.dumps(response.handler_result)}]})
    _write_message(output_stream, final)
elif response:
    _write_message(output_stream, response)
```

### 3. `tests/test_mcp_server.py`

New test class `TestStreamingGetContext` with three test methods:

- `test_handler_returns_stream_response`: mock store, call `_handle_get_context` with
  `stream=True` → assert `StreamResponse` returned, `sections` non-empty,
  `handler_result["context"]` non-empty.
- `test_http_streaming_response`: spin up a live test HTTP server, POST with
  `stream=True`, read response line-by-line → assert `ncp_chunk` lines followed by a
  valid JSON-RPC final line.
- `test_stdio_streaming_notifications`: call `serve_streams` on `BytesIO` streams with
  a `stream=True` request → parse output messages → assert notification frames before
  the final response frame.

---

## Wire formats

### HTTP (NDJSON over chunked Transfer-Encoding)

```
POST /mcp  Content-Type: application/json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"ncp_get_context","arguments":{..., "stream":true}}}

← HTTP/1.1 200 OK
   Content-Type: application/x-ndjson
   Transfer-Encoding: chunked

{"type":"ncp_chunk","section":"budget_header","index":0,"text":"NCP:BUDGET\n..."}\n
{"type":"ncp_chunk","section":"conscious","index":1,"text":"NCP:SYSTEM\n..."}\n
{"type":"ncp_chunk","section":"subconscious","index":2,"text":"NCP:SUBCONSCIOUS\n..."}\n
{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"context\":\"...\",\"session_id\":\"...\"}"}]}}\n
```

The last line is always the full JSON-RPC response. Clients that do not handle
NDJSON can parse only the last line. Clients that want progressive delivery read
chunks as they arrive.

### Stdio (JSON-RPC notifications + final response)

Each message is Content-Length-framed (unchanged from existing stdio protocol):

```
Content-Length: N\r\n\r\n
{"jsonrpc":"2.0","method":"ncp/stream_chunk","params":{"request_id":1,"section":"budget_header","index":0,"text":"..."}}

Content-Length: N\r\n\r\n
{"jsonrpc":"2.0","method":"ncp/stream_chunk","params":{"request_id":1,"section":"conscious","index":1,"text":"..."}}

Content-Length: N\r\n\r\n
{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"context\":\"...\"}"}]}}
```

Clients that do not handle `ncp/stream_chunk` notifications drop them silently
(standard JSON-RPC behavior) and still receive the final response.

---

## Backward compatibility

| Dimension | Impact |
|-----------|--------|
| Non-streaming callers | Zero change. `stream` defaults to `false`; handler takes existing branch. |
| `_handle_request` return type | Widens from `str` to `str \| StreamResponse`. Both callers already handle the `str` case; `isinstance` check added first. |
| `tools/list` response | `ncp_get_context` gains one optional property. Clients ignore unknown properties. |
| Existing tests | All pass; new test class is additive. |

---

## Out of scope (0.6.x Slice 1)

- Streaming `ncp_fetch` or `ncp_write_memory` — not needed.
- Back-pressure / cancellation — out of scope; connection close is sufficient.
- IVF-FLAT migration (004) — separate slice.
- Embedding provider integration — separate slice.
