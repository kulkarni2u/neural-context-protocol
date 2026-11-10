"""Security hardening of the MCP HTTP transport and server handlers.

Covers: Content-Type / Origin / Host (DNS rebinding) gating, negative
Content-Length, the per-connection socket timeout, the non-loopback auth
requirement in serve_http, ncp_record_outcome weight validation, collision-free
generated ids, and the src-based ceiling on caller-supplied base_trust.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import math
import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

import ncp.mcp.server as server_mod
from ncp.mcp.server import _handle_request, create_http_server, make_handlers, serve_http
from ncp.stores.sqlite import SQLiteStore


def _req(method: str, params: object | None = None, req_id: int = 1) -> dict:
    d: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        d["params"] = params
    return d


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _req("tools/call", params, req_id)


def _content(response_str: str) -> object:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


def _error(response_str: str) -> dict:
    return json.loads(response_str).get("error", {})


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Running:
    def __init__(self, server: server_mod._MCPHTTPServer, port: int) -> None:
        self.server = server
        self.port = port

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0)

    def raw(self, request: bytes, *, read_timeout: float = 5.0) -> bytes:
        """Send raw bytes and return everything the server writes back."""
        with closing(socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout)) as sock:
            sock.sendall(request)
            chunks: list[bytes] = []
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
                if b"\r\n\r\n" in b"".join(chunks):
                    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            if len(body) >= int(line.split(b":", 1)[1]):
                                return b"".join(chunks)
            return b"".join(chunks)


@pytest.fixture
def running(tmp_path: Path) -> Iterator[_Running]:
    with _run(tmp_path) as r:
        yield r


@contextmanager
def _run(tmp_path: Path, **kwargs: object) -> Iterator[_Running]:
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Running(server, port)
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _status(raw: bytes) -> int:
    return int(raw.split(b"\r\n", 1)[0].split()[1])


# ── Content-Type ─────────────────────────────────────────────────────────────


class TestContentType:
    def test_text_plain_post_is_rejected_with_415(self, running: _Running) -> None:
        with running.client() as client:
            resp = client.post(
                "/mcp",
                content=json.dumps(_req("tools/list")).encode(),
                headers={"Content-Type": "text/plain"},
            )
        assert resp.status_code == 415
        assert resp.json()["error"] == "unsupported_media_type"

    def test_form_and_missing_content_type_are_rejected(self, running: _Running) -> None:
        body = json.dumps(_req("tools/list")).encode()
        with running.client() as client:
            form = client.post(
                "/mcp", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            assert form.status_code == 415
            # httpx sends no Content-Type for a raw bytes body.
            missing = client.post("/mcp", content=body)
            assert missing.status_code == 415
            # /message is the other RPC path; it is gated the same way.
            message = client.post("/message", content=body, headers={"Content-Type": "text/plain"})
            assert message.status_code == 415

    @pytest.mark.parametrize(
        "content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"]
    )
    def test_json_content_type_is_accepted(self, running: _Running, content_type: str) -> None:
        with running.client() as client:
            resp = client.post(
                "/mcp",
                content=json.dumps(_req("tools/list")).encode(),
                headers={"Content-Type": content_type},
            )
        assert resp.status_code == 200
        assert resp.json()["result"]["tools"]

    def test_rejected_body_is_drained_so_keepalive_connection_stays_usable(self, running: _Running) -> None:
        body = json.dumps(_req("tools/list")).encode()
        with running.client() as client:
            assert client.post("/mcp", content=body, headers={"Content-Type": "text/plain"}).status_code == 415
            ok = client.post("/mcp", json=_req("tools/list"))
            assert ok.status_code == 200


# ── Origin ───────────────────────────────────────────────────────────────────


class TestOrigin:
    def test_foreign_origin_post_is_403_even_without_cors_config(self, running: _Running) -> None:
        with running.client() as client:
            resp = client.post("/mcp", json=_req("tools/list"), headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden_origin"
        assert "access-control-allow-origin" not in resp.headers

    def test_null_origin_is_rejected(self, running: _Running) -> None:
        with running.client() as client:
            resp = client.post("/mcp", json=_req("tools/list"), headers={"Origin": "null"})
        assert resp.status_code == 403

    def test_no_origin_post_still_works(self, running: _Running) -> None:
        with running.client() as client:
            resp = client.post("/mcp", json=_req("tools/list"))
        assert resp.status_code == 200

    def test_same_origin_is_allowed_for_bundled_ui(self, running: _Running) -> None:
        own = f"http://127.0.0.1:{running.port}"
        with running.client() as client:
            api = client.get("/api/status", headers={"Origin": own})
            assert api.status_code == 200
            rpc = client.post("/mcp", json=_req("tools/list"), headers={"Origin": own})
            assert rpc.status_code == 200
            # Same host, different port is a different origin.
            other_port = client.get("/api/status", headers={"Origin": "http://127.0.0.1:1"})
            assert other_port.status_code == 403

    def test_same_origin_via_localhost_host_header(self, running: _Running) -> None:
        own = f"http://localhost:{running.port}"
        with running.client() as client:
            resp = client.get(
                "/api/status", headers={"Origin": own, "Host": f"localhost:{running.port}"}
            )
        assert resp.status_code == 200

    def test_configured_origin_is_allowed(self, tmp_path: Path) -> None:
        with _run(tmp_path, cors_allowed_origins=["https://app.example"]) as running:
            with running.client() as client:
                ok = client.post("/mcp", json=_req("tools/list"), headers={"Origin": "https://app.example"})
                assert ok.status_code == 200
                assert ok.headers["access-control-allow-origin"] == "https://app.example"
                denied = client.post(
                    "/mcp", json=_req("tools/list"), headers={"Origin": "https://other.example"}
                )
                assert denied.status_code == 403

    def test_wildcard_cors_config_allows_any_origin(self, tmp_path: Path) -> None:
        with _run(tmp_path, cors_allowed_origins=["*"]) as running:
            with running.client() as client:
                ok = client.post("/mcp", json=_req("tools/list"), headers={"Origin": "https://any.example"})
                assert ok.status_code == 200

    def test_data_bearing_gets_reject_foreign_origin(self, running: _Running) -> None:
        evil = {"Origin": "https://evil.example"}
        with running.client() as client:
            assert client.get("/api/status", headers=evil).status_code == 403
            assert client.get("/api/overview", headers=evil).status_code == 403
            assert client.get("/events?agent_id=a", headers=evil).status_code == 403
            assert client.get("/sse", headers=evil).status_code == 403
            # Non-data endpoints are not origin-gated.
            assert client.get("/healthz", headers=evil).status_code == 200
            # No Origin keeps working on the data endpoints.
            assert client.get("/api/status").status_code == 200


# ── Host (DNS rebinding) ─────────────────────────────────────────────────────


class TestHost:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
    def test_loopback_host_values_are_accepted_on_any_port(self, running: _Running, host: str) -> None:
        with running.client() as client:
            for value in (host, f"{host}:{running.port}", f"{host}:9999"):
                resp = client.post("/mcp", json=_req("tools/list"), headers={"Host": value})
                assert resp.status_code == 200, value

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:4242", "127.0.0.1.evil.example", "10.0.0.5"])
    def test_foreign_host_is_rejected_on_loopback_bind(self, running: _Running, host: str) -> None:
        with running.client() as client:
            rpc = client.post("/mcp", json=_req("tools/list"), headers={"Host": host})
            assert rpc.status_code == 403
            assert rpc.json()["error"] == "forbidden_host"
            assert client.get("/api/status", headers={"Host": host}).status_code == 403
            assert client.get("/healthz", headers={"Host": host}).status_code == 403
            assert client.get("/ui/", headers={"Host": host}).status_code == 403
            assert client.options("/mcp", headers={"Host": host}).status_code == 403

    def test_rebinding_same_origin_pair_is_still_rejected(self, running: _Running) -> None:
        """A rebound page has Origin == Host == the attacker's name; Host wins."""
        headers = {"Host": f"evil.example:{running.port}", "Origin": f"http://evil.example:{running.port}"}
        with running.client() as client:
            resp = client.post("/mcp", json=_req("tools/list"), headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden_host"

    def test_host_is_not_restricted_on_non_loopback_bind(self, running: _Running) -> None:
        running.server.loopback_bound = False
        with running.client() as client:
            resp = client.post("/mcp", json=_req("tools/list"), headers={"Host": "ncp.internal:4242"})
        assert resp.status_code == 200

    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]", "LocalHost"):
            assert server_mod._is_loopback_host(host), host
        for host in ("0.0.0.0", "::", "", "10.0.0.1", "192.168.1.2", "example.com"):
            assert not server_mod._is_loopback_host(host), host


# ── Content-Length ───────────────────────────────────────────────────────────


class TestContentLength:
    def test_negative_content_length_is_400(self, running: _Running) -> None:
        raw = running.raw(
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: -1\r\n\r\n" + json.dumps(_req("tools/list")).encode()
        )
        assert _status(raw) == 400
        assert b"invalid_content_length" in raw

    def test_non_numeric_content_length_is_400(self, running: _Running) -> None:
        raw = running.raw(
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: abc\r\n\r\n{}"
        )
        assert _status(raw) == 400

    def test_negative_content_length_on_rejected_request_does_not_hang(self, running: _Running) -> None:
        # Early-rejection paths drain the body; a negative length must not read to EOF.
        start = time.monotonic()
        raw = running.raw(
            b"POST /nope HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n",
            read_timeout=5.0,
        )
        assert _status(raw) == 404
        assert time.monotonic() - start < 4.0


# ── Socket timeout ───────────────────────────────────────────────────────────


class TestSocketTimeout:
    def test_default_timeout_is_set(self, running: _Running) -> None:
        assert running.server.socket_timeout_seconds == server_mod._HTTP_SOCKET_TIMEOUT_SECONDS

    def test_stalled_client_is_disconnected(self, tmp_path: Path) -> None:
        with _run(tmp_path, socket_timeout_seconds=0.5) as running:
            with closing(socket.create_connection(("127.0.0.1", running.port), timeout=10)) as sock:
                # Headers promise a body that never arrives.
                sock.sendall(
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n{"
                )
                start = time.monotonic()
                assert sock.recv(65536) == b""  # server closed the connection
                assert time.monotonic() - start < 5.0

    def test_partial_request_line_is_disconnected(self, tmp_path: Path) -> None:
        with _run(tmp_path, socket_timeout_seconds=0.5) as running:
            with closing(socket.create_connection(("127.0.0.1", running.port), timeout=10)) as sock:
                sock.sendall(b"POST /mcp HT")
                start = time.monotonic()
                assert sock.recv(65536) == b""
                assert time.monotonic() - start < 5.0

    def test_sse_stream_outlives_socket_timeout(self, tmp_path: Path) -> None:
        with _run(tmp_path, socket_timeout_seconds=0.3, keepalive_seconds=0.05) as running:
            with running.client() as client:
                with client.stream("GET", "/sse") as resp:
                    assert resp.status_code == 200
                    received = b""
                    deadline = time.monotonic() + 1.2
                    for chunk in resp.iter_bytes():
                        received += chunk
                        if time.monotonic() > deadline:
                            break
                    assert time.monotonic() > deadline  # stream stayed open past the timeout
                    assert b"event: endpoint" in received
                    assert received.count(b": keepalive") >= 3

    def test_blocking_wait_whispers_outlives_socket_timeout(self, tmp_path: Path) -> None:
        with _run(tmp_path, socket_timeout_seconds=0.3) as running:
            with running.client() as client:
                start = time.monotonic()
                resp = client.post(
                    "/mcp",
                    json=_call("ncp_wait_whispers", {"agent_id": "waiter", "timeout_seconds": 1.0}),
                )
                elapsed = time.monotonic() - start
            assert resp.status_code == 200
            payload = json.loads(resp.json()["result"]["content"][0]["text"])
            assert payload["ready"] is False
            assert payload["timed_out"] is True
            assert elapsed >= 0.9


# ── serve_http non-loopback auth requirement ─────────────────────────────────


class TestServeHttpAuthRequirement:
    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.1.2.3", "ncp.internal"])
    def test_non_loopback_without_token_refuses_to_start(
        self, host: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _never(**_: object) -> None:
            raise AssertionError("create_http_server must not be called")

        monkeypatch.setattr(server_mod, "create_http_server", _never)
        with pytest.raises(SystemExit) as exc:
            serve_http(host=host, port=0, auth_token=None)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "refusing to bind" in err
        assert "auth token" in err

    def test_empty_token_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod, "create_http_server", lambda **_: pytest.fail("must not start"))
        with pytest.raises(SystemExit):
            serve_http(host="0.0.0.0", port=0, auth_token="")

    @pytest.mark.parametrize(
        ("host", "token"),
        [("0.0.0.0", "secret"), ("127.0.0.1", None), ("localhost", None), ("::1", None)],
    )
    def test_allowed_configurations_start(
        self, host: str, token: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: dict[str, object] = {}

        class _FakeServer:
            _shutdown_event = threading.Event()

            def serve_forever(self) -> None:
                started["served"] = True

            def server_close(self) -> None:
                started["closed"] = True

        def _fake_create(**kwargs: object) -> _FakeServer:
            started.update(kwargs)
            return _FakeServer()

        monkeypatch.setattr(server_mod, "create_http_server", _fake_create)
        serve_http(host=host, port=0, auth_token=token)
        assert started["served"] is True
        assert started["host"] == host
        assert started["auth_token"] == token


# ── ncp_record_outcome weight ────────────────────────────────────────────────


class TestRecordOutcomeWeight:
    @pytest.mark.parametrize(
        "weight", [float("nan"), float("inf"), float("-inf"), 0, 0.0, -0.5, 1.0000001, 2, 1e9, "0.5", True, [1]]
    )
    def test_invalid_weight_is_rejected(self, tmp_path: Path, weight: object) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_record_outcome", {"chunk_ids": ["c1"], "success": True, "weight": weight}),
            handlers,
        )
        err = _error(resp)
        assert err, resp
        assert "weight" in err.get("message", "")
        assert store.list_outcomes() == []

    @pytest.mark.parametrize(("weight", "stored"), [(0.5, 0.5), (1, 1.0), (1.0, 1.0), (1e-6, 1e-6), (None, 1.0)])
    def test_valid_weight_is_stored(self, tmp_path: Path, weight: object, stored: float) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        result = _content(_handle_request(
            _call("ncp_record_outcome", {"chunk_ids": ["c1"], "success": True, "weight": weight}),
            handlers,
        ))
        assert result["recorded"] is True
        outcome = store.get_outcome(result["outcome_id"])
        assert outcome is not None
        assert math.isclose(outcome.weight, stored)

    def test_omitted_weight_defaults_to_one(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        result = _content(_handle_request(
            _call("ncp_record_outcome", {"chunk_ids": ["c1"], "success": False}), handlers
        ))
        outcome = store.get_outcome(result["outcome_id"])
        assert outcome is not None and outcome.weight == 1.0

    def test_schema_documents_range(self) -> None:
        tool = next(t for t in server_mod.MCP_TOOLS if t["name"] == "ncp_record_outcome")
        desc = tool["inputSchema"]["properties"]["weight"]["description"]
        assert "0 < weight <= 1" in desc


# ── Collision-free generated ids ─────────────────────────────────────────────


@pytest.fixture
def frozen_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call lands in the same millisecond."""
    monkeypatch.setattr(server_mod.time, "time", lambda: 1_700_000_000.123)


class TestGeneratedIds:
    def test_outcome_ids_do_not_collide_in_same_millisecond(self, tmp_path: Path, frozen_ms: None) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        ids = set()
        for _ in range(5):
            result = _content(_handle_request(
                _call("ncp_record_outcome", {"chunk_ids": ["c1"], "success": True}), handlers
            ))
            assert result["recorded"] is True  # a duplicate id would be INSERT OR IGNOREd
            assert result["outcome_id"].startswith("out_")
            ids.add(result["outcome_id"])
        assert len(ids) == 5
        assert len(store.list_outcomes()) == 5

    def test_turn_ids_do_not_collide_in_same_millisecond(self, tmp_path: Path, frozen_ms: None) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        ids = set()
        for i in range(3):
            result = _content(_handle_request(
                _call("ncp_post_turn", {
                    "agent_id": f"agent_{i}",
                    "role": "build",
                    "task": "t",
                    "slot": "s",
                    "intent": "i",
                    "result_summary": f"summary {i}",
                    "result_full": f"full {i}",
                }),
                handlers,
            ))
            assert result["turn_id"].startswith("turn_")
            ids.add(result["turn_id"])
        assert len(ids) == 3
        assert {t.turn_id for t in store.list_turns(limit=10)} >= ids

    def test_raw_ref_ids_do_not_collide_in_same_millisecond(self, tmp_path: Path, frozen_ms: None) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        noisy = "unique_marker line\nunique_marker line\nunique_marker line\nresult"
        refs = set()
        for _ in range(2):
            result = _content(_handle_request(
                _call("ncp_write_memory", {"content": noisy, "layer": "episodic", "src": "tool_result"}),
                handlers,
            ))
            assert result["raw_ref"].startswith("raw_")
            refs.add(result["raw_ref"])
        assert len(refs) == 2

    def test_explicit_ids_are_kept(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        result = _content(_handle_request(
            _call("ncp_record_outcome", {"chunk_ids": ["c1"], "success": True, "outcome_id": "out_custom"}),
            handlers,
        ))
        assert result["outcome_id"] == "out_custom"


# ── base_trust ceiling ───────────────────────────────────────────────────────


class TestBaseTrustCeiling:
    def _write(self, store: SQLiteStore, **args: object) -> float:
        handlers = make_handlers(store)
        result = _content(_handle_request(
            _call("ncp_write_memory", {"content": "a plain fact", "layer": "semantic", **args}),
            handlers,
        ))
        chunk = next(c for c in store.get_working_zone() if c.chunk_id == result["chunk_id"])
        return chunk.base_trust

    @pytest.mark.parametrize(
        ("src", "ceiling"),
        [
            ("agent_inferred", 0.60),
            ("synthesis", 0.70),
            ("subcon_retrieved", 0.55),
            ("skill_ref", 0.60),
            ("tool_result", 0.80),
            ("user_verified", 0.95),
        ],
    )
    def test_explicit_base_trust_cannot_exceed_src_default(
        self, tmp_path: Path, src: str, ceiling: float
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        assert self._write(store, src=src, base_trust=1.0) == pytest.approx(ceiling)

    def test_explicit_lower_base_trust_is_kept(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        assert self._write(store, src="tool_result", base_trust=0.42) == pytest.approx(0.42)

    def test_unknown_src_is_capped_at_fallback(self) -> None:
        assert server_mod._trust_from_args({"src": "made_up", "base_trust": 0.99}) == pytest.approx(0.70)
        assert server_mod._trust_from_args({"base_trust": 0.99}) == pytest.approx(0.70)

    def test_out_of_range_and_nan_base_trust(self) -> None:
        assert server_mod._trust_from_args({"src": "tool_result", "base_trust": -3}) == 0.0
        assert server_mod._trust_from_args({"src": "tool_result", "base_trust": float("nan")}) == 0.0
        assert server_mod._trust_from_args({"src": "tool_result", "base_trust": "junk"}) == pytest.approx(0.80)

    def test_post_turn_memory_chunks_are_capped_too(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        handlers = make_handlers(store)
        _handle_request(
            _call("ncp_post_turn", {
                "agent_id": "builder",
                "role": "build",
                "task": "t",
                "slot": "s",
                "intent": "i",
                "turn_id": "turn_cap",
                "result_summary": "summary",
                "result_full": "full",
                "memory_chunks": [
                    {"content": "inferred claim", "layer": "semantic", "src": "agent_inferred", "base_trust": 0.99}
                ],
            }),
            handlers,
        )
        chunks = [c for c in store.get_working_zone() if c.content == "inferred claim"]
        assert chunks and chunks[0].base_trust == pytest.approx(0.60)
