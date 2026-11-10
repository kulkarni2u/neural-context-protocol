"""HTTP-level enforcement of opt-in authenticated principals.

Each class covers one rule from docs/NCP_HTTP_API.md "Principals":
authentication, identity binding (including the conscious-state overwrite
attack), the pipeline ACL, capabilities, the principal-keyed fetch budget, the
GET endpoints, the legacy-token interaction, and the unchanged
no-principals path.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import httpx
import pytest

import ncp.mcp.server as server_mod
from ncp.mcp.server import FORBIDDEN_ERROR_CODE, create_http_server, serve_http
from ncp.principals import PrincipalConfigError, hash_token
from ncp.stores.sqlite import SQLiteStore

TOKENS = {"alice": "tok-alice", "bob": "tok-bob", "router": "tok-router", "validator": "tok-validator"}

PRINCIPALS_TOML = f"""
[tasks]
enabled = true

[principals.alice]
token_sha256 = "{hash_token(TOKENS["alice"])}"
agent_id = "alice"

[principals.bob]
token_env = "NCP_TEST_BOB_TOKEN"
agent_id = "bob"
pipelines = ["p1"]

[principals.router]
token_sha256 = "{hash_token(TOKENS["router"])}"
agent_id = "router"
capabilities = ["admin", "verify", "record_outcome"]

[principals.validator]
token_sha256 = "{hash_token(TOKENS["validator"])}"
agent_id = "validator"
capabilities = ["record_outcome"]
"""


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    def __init__(self, server: server_mod._MCPHTTPServer, port: int, root: Path) -> None:
        self.server = server
        self.port = port
        self.root = root
        self._ids = 0

    def http(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0)

    def rpc(self, method: str, params: dict | None = None, *, token: str | None) -> httpx.Response:
        self._ids += 1
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}}
        with self.http() as client:
            return client.post("/mcp", content=json.dumps(body), headers=headers)

    def call(self, name: str, arguments: dict, *, as_: str | None) -> dict:
        token = None if as_ is None else TOKENS.get(as_, as_)
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, token=token)
        assert response.status_code == 200, response.text
        return response.json()

    def ok(self, name: str, arguments: dict, *, as_: str | None) -> dict:
        payload = self.call(name, arguments, as_=as_)
        assert "error" not in payload, payload
        return json.loads(payload["result"]["content"][0]["text"])

    def forbidden(self, name: str, arguments: dict, *, as_: str | None, match: str = "") -> str:
        payload = self.call(name, arguments, as_=as_)
        assert "error" in payload, payload
        assert payload["error"]["code"] == FORBIDDEN_ERROR_CODE, payload
        message = str(payload["error"]["message"])
        assert message.startswith("Forbidden: ")
        assert match in message, message
        return message

    def get(self, path: str, *, as_: str | None) -> httpx.Response:
        headers = {} if as_ is None else {"Authorization": f"Bearer {TOKENS.get(as_, as_)}"}
        with self.http() as client:
            return client.get(path, headers=headers)

    def store(self) -> SQLiteStore:
        return SQLiteStore(self.root / ".ncp" / "store.db")


@contextmanager
def _serve(root: Path, config_toml: str | None, **kwargs: object) -> Iterator[_Server]:
    if config_toml is not None:
        (root / ".ncp").mkdir(exist_ok=True)
        (root / ".ncp" / "config.toml").write_text(config_toml, encoding="utf-8")
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(server, port, root)
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    monkeypatch.setenv("NCP_TEST_BOB_TOKEN", TOKENS["bob"])
    with _serve(tmp_path, PRINCIPALS_TOML) as running:
        yield running


def _ctx(agent_id: str, **extra: object) -> dict:
    return {"agent_id": agent_id, "role": "build", "task": "fix_bug", "slot": "payment", "intent": "advance", **extra}


def _turn(agent_id: str, **extra: object) -> dict:
    return {**_ctx(agent_id), "result_summary": "done", "result_full": "done in full", **extra}


# ── rule 1: authentication ───────────────────────────────────────────────────


class TestAuthentication:
    def test_missing_or_unknown_token_is_401_even_on_loopback(self, srv: _Server) -> None:
        assert srv.rpc("tools/list", token=None).status_code == 401
        assert srv.rpc("tools/list", token="not-a-token").status_code == 401
        assert srv.get("/api/status?pipeline_id=p1", as_=None).status_code == 401
        assert srv.get("/events?agent_id=alice", as_="wrong").status_code == 401

    def test_valid_tokens_authenticate(self, srv: _Server) -> None:
        for name in TOKENS:
            response = srv.rpc("tools/list", token=TOKENS[name])
            assert response.status_code == 200
            assert response.json()["result"]["tools"]

    def test_env_token_principal_works(self, srv: _Server) -> None:
        srv.ok("ncp_get_context", _ctx("bob", pipeline_id="p1"), as_="bob")

    def test_invalid_principals_config_refuses_to_start(self, tmp_path: Path) -> None:
        (tmp_path / ".ncp").mkdir()
        (tmp_path / ".ncp" / "config.toml").write_text(
            '[principals.x]\nagent_id = "x"\ntoken = "plaintext"\n', encoding="utf-8"
        )
        with pytest.raises(PrincipalConfigError, match="plaintext"):
            create_http_server(host="127.0.0.1", port=_free_port(), cwd=tmp_path)

    def test_missing_token_env_refuses_to_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NCP_TEST_BOB_TOKEN", raising=False)
        (tmp_path / ".ncp").mkdir()
        (tmp_path / ".ncp" / "config.toml").write_text(PRINCIPALS_TOML, encoding="utf-8")
        with pytest.raises(PrincipalConfigError, match="NCP_TEST_BOB_TOKEN"):
            create_http_server(host="127.0.0.1", port=_free_port(), cwd=tmp_path)


# ── rule 2: identity comes from the principal ────────────────────────────────


class TestIdentityBinding:
    def test_attack_a_conscious_overwrite_is_rejected(self, srv: _Server) -> None:
        """Review finding (a): B rewriting A's owns/must_not via ncp_post_turn."""
        srv.ok(
            "ncp_post_turn",
            _turn("alice", owns=["payments"], must_not=["touch_auth"]),
            as_="alice",
        )
        message = srv.forbidden(
            "ncp_post_turn",
            _turn("alice", owns=["everything"], must_not=[]),
            as_="validator",
            match="agent_id='alice' does not match",
        )
        assert "validator" in message
        # get_context, which also persists conscious state, is bound too.
        srv.forbidden("ncp_get_context", _ctx("alice", owns=["everything"]), as_="validator")
        latest = srv.store().load_latest_conscious(pipeline_id=None, agent_id="alice")
        assert latest is not None
        assert latest.owns == ["payments"]
        assert latest.must_not == ["touch_auth"]

    def test_admin_may_act_as_another_agent(self, srv: _Server) -> None:
        srv.ok("ncp_post_turn", _turn("alice", owns=["router_assigned"]), as_="router")
        latest = srv.store().load_latest_conscious(pipeline_id=None, agent_id="alice")
        assert latest is not None and latest.owns == ["router_assigned"]

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("ncp_write_memory", {"content": "x", "layer": "semantic", "src": "synthesis", "written_by": "bob"}),
            ("ncp_emit_whisper", {"from": "bob", "target": "x", "type": "fyi", "payload": "p", "confidence": 0.9}),
            ("ncp_wait_whispers", {"agent_id": "bob", "timeout_seconds": 0}),
            ("ncp_overview", {"agent_id": "bob"}),
            ("ncp_fetch", {"agent_id": "bob", "query": "q"}),
            ("ncp_record_decision", {"decision": "d", "rationale": "r", "agent_id": "bob"}),
            ("ncp_task", {"action": "create", "agent_id": "bob", "pipeline_id": "p1", "slot": "s", "title": "t"}),
            ("ncp_post_turn", _turn("alice", memory_chunks=[{"content": "c", "layer": "semantic", "src": "synthesis", "written_by": "bob"}])),
        ],
    )
    def test_declared_identity_must_match(self, srv: _Server, tool: str, args: dict) -> None:
        srv.forbidden(tool, args, as_="alice", match="does not match")

    def test_omitted_identity_is_filled_from_principal(self, srv: _Server) -> None:
        written = srv.ok("ncp_write_memory", {"content": "alice fact", "layer": "semantic", "src": "synthesis"}, as_="alice")
        chunks = srv.store().get_chunks_by_ids([written["chunk_id"]])
        assert chunks and chunks[0].written_by == "alice"

    def test_overwriting_another_agents_chunk_is_rejected(self, srv: _Server) -> None:
        written = srv.ok("ncp_write_memory", {"content": "original", "layer": "semantic", "src": "synthesis"}, as_="alice")
        srv.forbidden(
            "ncp_write_memory",
            {"content": "hijacked", "layer": "semantic", "src": "synthesis", "chunk_id": written["chunk_id"]},
            as_="validator",
            match="only its author",
        )
        assert srv.store().get_chunks_by_ids([written["chunk_id"]])[0].content == "original"


# ── rule 3: pipeline ACL ─────────────────────────────────────────────────────


class TestPipelineAcl:
    def test_allowed_pipeline_works(self, srv: _Server) -> None:
        srv.ok("ncp_write_memory", {"content": "p1 fact", "layer": "semantic", "src": "synthesis", "pipeline_id": "p1"}, as_="bob")

    def test_other_pipeline_is_rejected(self, srv: _Server) -> None:
        srv.forbidden(
            "ncp_write_memory",
            {"content": "x", "layer": "semantic", "src": "synthesis", "pipeline_id": "p2"},
            as_="bob",
            match="may not access pipeline 'p2'",
        )
        srv.forbidden("ncp_recall", {"query": "q", "pipeline_id": "p2"}, as_="bob")

    def test_omitted_pipeline_is_rejected_not_defaulted(self, srv: _Server) -> None:
        srv.forbidden(
            "ncp_write_memory",
            {"content": "x", "layer": "semantic", "src": "synthesis"},
            as_="bob",
            match="pipeline_id is required",
        )
        srv.forbidden("ncp_get_context", _ctx("bob"), as_="bob", match="pipeline_id is required")

    def test_task_by_id_is_scoped_by_the_tasks_pipeline(self, srv: _Server) -> None:
        created = srv.ok(
            "ncp_task",
            {"action": "create", "pipeline_id": "p2", "slot": "s", "title": "t"},
            as_="alice",
        )
        task_id = created["task"]["task_id"]
        srv.forbidden("ncp_task", {"action": "get", "task_id": task_id}, as_="bob", match="'p2'")
        srv.forbidden("ncp_task", {"action": "claim", "task_id": task_id}, as_="bob", match="pipeline_id is required")

    def test_get_decision_outside_pipelines_reads_as_not_found(self, srv: _Server) -> None:
        recorded = srv.ok(
            "ncp_record_decision",
            {"decision": "use_x", "rationale": "r", "pipeline_id": "p2", "schema_id": "legacy.freeform", "choice": "use_x"},
            as_="alice",
        )
        decision_id = recorded["decision_id"]
        assert srv.ok("ncp_get_decision", {"decision_id": decision_id}, as_="alice")["found"] is True
        assert srv.ok("ncp_get_decision", {"decision_id": decision_id}, as_="bob") == {
            "found": False,
            "decision_id": decision_id,
        }


# ── rule 4: capabilities and self-crediting ──────────────────────────────────


class TestCapabilities:
    def test_user_verified_requires_verify(self, srv: _Server) -> None:
        srv.forbidden(
            "ncp_write_memory",
            {"content": "x", "layer": "semantic", "src": "user_verified"},
            as_="alice",
            match="'verify' capability",
        )
        srv.forbidden(
            "ncp_post_turn",
            _turn("alice", memory_chunks=[{"content": "c", "layer": "semantic", "src": "user_verified"}]),
            as_="alice",
            match="'verify' capability",
        )
        srv.ok("ncp_write_memory", {"content": "x", "layer": "semantic", "src": "user_verified"}, as_="router")

    def test_record_outcome_requires_capability(self, srv: _Server) -> None:
        written = srv.ok("ncp_write_memory", {"content": "bob fact", "layer": "semantic", "src": "synthesis", "pipeline_id": "p1"}, as_="bob")
        srv.forbidden(
            "ncp_record_outcome",
            {"success": True, "chunk_ids": [written["chunk_id"]]},
            as_="alice",
            match="'record_outcome' capability",
        )
        recorded = srv.ok("ncp_record_outcome", {"success": True, "chunk_ids": [written["chunk_id"]]}, as_="validator")
        assert recorded["recorded"] is True

    def test_self_crediting_is_rejected(self, srv: _Server) -> None:
        own = srv.ok("ncp_write_memory", {"content": "validator fact", "layer": "semantic", "src": "synthesis"}, as_="validator")
        srv.forbidden(
            "ncp_record_outcome",
            {"success": True, "chunk_ids": [own["chunk_id"]]},
            as_="validator",
            match="its own chunks",
        )
        assert srv.store().list_outcomes(chunk_ids=[own["chunk_id"]]) == []

    def test_turn_only_outcome_requires_admin(self, srv: _Server) -> None:
        posted = srv.ok("ncp_post_turn", _turn("alice"), as_="alice")
        srv.forbidden(
            "ncp_record_outcome", {"success": True, "turn_id": posted["turn_id"]}, as_="validator", match="explicit chunk_ids"
        )
        assert srv.ok("ncp_record_outcome", {"success": True, "turn_id": posted["turn_id"]}, as_="router")["recorded"] is True

    def test_admin_may_self_credit(self, srv: _Server) -> None:
        own = srv.ok("ncp_write_memory", {"content": "router fact", "layer": "semantic", "src": "synthesis"}, as_="router")
        srv.ok("ncp_record_outcome", {"success": True, "chunk_ids": [own["chunk_id"]]}, as_="router")


# ── rule 5: fetch budget keyed by principal ──────────────────────────────────


class TestFetchBudget:
    def test_rotating_session_id_does_not_reset_the_budget(self, srv: _Server) -> None:
        context = srv.ok("ncp_get_context", _ctx("alice", session_id="mine"), as_="alice")
        assert context["session_id"] == "principal:alice"
        for i in range(3):
            srv.ok("ncp_fetch", {"query": "q", "session_id": f"fresh-{i}"}, as_="alice")
        payload = srv.call("ncp_fetch", {"query": "q", "session_id": "fresh-again"}, as_="alice")
        assert "limit reached" in payload["error"]["message"]
        # A new turn (ncp_get_context) restores the budget, as before.
        srv.ok("ncp_get_context", _ctx("alice"), as_="alice")
        srv.ok("ncp_fetch", {"query": "q"}, as_="alice")

    def test_budgets_are_per_principal(self, srv: _Server) -> None:
        srv.ok("ncp_get_context", _ctx("alice"), as_="alice")
        for _ in range(3):
            srv.ok("ncp_fetch", {"query": "q"}, as_="alice")
        srv.ok("ncp_get_context", _ctx("validator"), as_="validator")
        srv.ok("ncp_fetch", {"query": "q"}, as_="validator")


# ── rule 6: GET endpoints ────────────────────────────────────────────────────


class TestReadEndpoints:
    def test_unscoped_reads_require_admin(self, srv: _Server) -> None:
        for path in ("/api/chunks", "/api/status", "/api/turns", "/api/whispers", "/api/graph", "/api/cost", "/api/overview"):
            response = srv.get(path, as_="alice")
            assert response.status_code == 403, path
            assert response.json()["error"] == "forbidden"
            assert srv.get(path, as_="router").status_code == 200, path

    def test_scoped_reads_follow_the_pipeline_acl(self, srv: _Server) -> None:
        assert srv.get("/api/chunks?pipeline_id=p1", as_="bob").status_code == 200
        assert srv.get("/api/chunks?pipeline_id=p2", as_="bob").status_code == 403
        assert srv.get("/api/chunks", as_="bob").status_code == 403
        assert srv.get("/api/chunks?pipeline_id=p2", as_="alice").status_code == 200
        assert srv.get("/api/overview", as_="bob").status_code == 403

    def test_events_agent_id_is_bound(self, srv: _Server) -> None:
        response = srv.get("/events?agent_id=alice", as_="validator")
        assert response.status_code == 403
        assert "does not match" in response.json()["detail"]
        assert srv.get("/events?agent_id=bob&pipeline_id=p2", as_="bob").status_code == 403
        assert srv.get("/events?agent_id=bob", as_="bob").status_code == 403
        # Admin must still name an agent.
        assert srv.get("/events", as_="router").status_code == 400

    def test_events_fills_agent_id_from_principal(self, srv: _Server) -> None:
        headers = {"Authorization": f"Bearer {TOKENS['alice']}"}
        with srv.http() as client, client.stream("GET", "/events", headers=headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

    def test_sse_requires_a_principal(self, srv: _Server) -> None:
        assert srv.get("/sse", as_=None).status_code == 401


# ── legacy token interaction ─────────────────────────────────────────────────


class TestLegacyToken:
    def test_legacy_token_is_implicit_admin_when_principals_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NCP_TEST_BOB_TOKEN", TOKENS["bob"])
        with _serve(tmp_path, PRINCIPALS_TOML, auth_token="shared") as running:
            running.ok("ncp_post_turn", _turn("alice"), as_="shared")
            running.ok("ncp_write_memory", {"content": "x", "layer": "semantic", "src": "user_verified"}, as_="shared")
            assert running.get("/api/chunks", as_="shared").status_code == 200
            # Named principals are still bound.
            running.forbidden("ncp_post_turn", _turn("alice"), as_="validator")
            assert running.rpc("tools/list", token=None).status_code == 401

    def test_principal_token_reusing_legacy_token_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCP_TEST_BOB_TOKEN", TOKENS["bob"])
        (tmp_path / ".ncp").mkdir()
        (tmp_path / ".ncp" / "config.toml").write_text(PRINCIPALS_TOML, encoding="utf-8")
        with pytest.raises(PrincipalConfigError, match="auth_token"):
            create_http_server(host="127.0.0.1", port=_free_port(), cwd=tmp_path, auth_token=TOKENS["alice"])


# ── no principals: behaviour unchanged ───────────────────────────────────────


class TestNoPrincipals:
    def test_open_loopback_trusts_declared_identity(self, tmp_path: Path) -> None:
        with _serve(tmp_path, None) as running:
            assert running.server.principals is None
            running.ok("ncp_post_turn", _turn("alice", owns=["a"]), as_=None)
            running.ok("ncp_post_turn", _turn("alice", owns=["b"]), as_=None)
            running.ok("ncp_write_memory", {"content": "x", "layer": "semantic", "src": "user_verified", "written_by": "anyone"}, as_=None)
            assert running.get("/api/chunks", as_=None).status_code == 200
            context = running.ok("ncp_get_context", _ctx("alice", session_id="caller_chosen"), as_=None)
            assert context["session_id"] == "caller_chosen"

    def test_shared_token_still_required_and_sufficient(self, tmp_path: Path) -> None:
        with _serve(tmp_path, None, auth_token="shared") as running:
            assert running.rpc("tools/list", token=None).status_code == 401
            running.ok("ncp_post_turn", _turn("whoever"), as_="shared")


# ── serve_http startup ───────────────────────────────────────────────────────


def test_non_loopback_with_principals_and_no_token_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".ncp").mkdir()
    (tmp_path / ".ncp" / "config.toml").write_text(
        f'[principals.a]\nagent_id = "a"\ntoken_sha256 = "{hash_token("t")}"\n', encoding="utf-8"
    )
    started: dict[str, object] = {}

    class _FakeServer:
        _shutdown_event = threading.Event()
        principals = None

        def serve_forever(self) -> None:
            started["served"] = True

        def server_close(self) -> None:
            started["closed"] = True

    monkeypatch.setattr(server_mod, "create_http_server", lambda **kw: started.update(kw) or _FakeServer())
    serve_http(host="0.0.0.0", port=0, cwd=tmp_path, auth_token=None)
    assert started["served"] is True


def test_stdio_dispatch_has_no_principal(tmp_path: Path) -> None:
    """stdio is unauthenticated-local: _handle_request without a principal is unchanged."""
    from ncp.mcp.server import _handle_request, make_handlers

    handlers = make_handlers(SQLiteStore(tmp_path / "s.db"))
    response = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": _turn("anyone")}},
        handlers,
    )
    assert isinstance(response, str) and '"error"' not in response
