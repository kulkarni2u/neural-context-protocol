"""ncp_skill MCP tool, skill poisoning guard, HTTP skill registration, get_context skills."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from ncp.config import load_config
from ncp.mcp.server import (
    MCP_TOOLS,
    _handle_request,
    create_http_server,
    make_handlers,
    narrow_tools,
    tools_for_config,
)
from ncp.principals import hash_token
from ncp.skill_cache import _MANIFEST_SECTION_INDEX, _section_chunk_id, cache_skill, _content_hash
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

CTX = {"agent_id": "worker", "role": "build", "task": "t", "slot": "s", "intent": "i", "pipeline_id": "p1"}
SKILL = "acme/deploy"
BODY = "# Deploy\n\nStep 1: run tests.\n\néè Step 2: ship it.\n\n```sh\nmake ship\n```"
LONG_REF = ("Checklist item about database migrations and rollback safety. " * 12 + "\n\n") * 6


def _write_config(root: Path, toml: str) -> None:
    (root / ".ncp").mkdir(exist_ok=True)
    (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")


class _Env:
    def __init__(self, root: Path, *, enabled: bool = True, profile: str = "full") -> None:
        extra = "[skill_cache]\nenabled = true\n" if enabled else ""
        _write_config(root, f'[tools]\nprofile = "{profile}"\n{extra}')
        self.config = load_config(cwd=root)
        self.store = SQLiteStore(root / "s.db")
        self.tools = tools_for_config(self.config)
        self.handlers = make_handlers(self.store, config=self.config)

    def raw(self, name: str, arguments: dict) -> dict:
        return json.loads(_handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            self.handlers, tools=self.tools,
        ))

    def call(self, name: str, arguments: dict) -> dict:
        payload = self.raw(name, arguments)
        assert "error" not in payload, payload
        return json.loads(payload["result"]["content"][0]["text"])

    def skill(self, **args: object) -> dict:
        return self.call("ncp_skill", {"session_id": "sess_a", **args})

    def register(self, content: str = BODY, skill_id: str = SKILL, **kwargs: object) -> str:
        return cache_skill(
            content, skill_id=skill_id, store=self.store, config=self.config, registered_by="user", **kwargs
        ).content_hash


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


class TestGating:
    def test_enabled_full_profile_only(self, tmp_path: Path) -> None:
        for profile, expected in (("full", True), ("standard", False), ("core", False), ("minimal", False)):
            e = _Env(tmp_path, profile=profile)
            assert ("ncp_skill" in {t["name"] for t in e.tools}) is expected
        full = _Env(tmp_path, profile="full")
        # A connection narrowing to standard loses it; the server set never widens.
        assert "ncp_skill" not in {t["name"] for t in narrow_tools(full.tools, profile="standard")}

    def test_disabled_absent_and_rejected(self, tmp_path: Path) -> None:
        e = _Env(tmp_path, enabled=False)
        assert "ncp_skill" not in {t["name"] for t in e.tools}
        assert "skills" not in json.dumps(next(t for t in e.tools if t["name"] == "ncp_get_context"))
        resp = e.raw("ncp_skill", {"action": "list"})
        assert resp["error"]["code"] == -32601
        # Even with every handler exposed, the handler itself refuses.
        with pytest.raises(ValueError, match="disabled"):
            e.handlers["ncp_skill"]({"action": "list"})

    def test_catalog_size_cost_is_bounded(self, tmp_path: Path) -> None:
        def size(e: _Env) -> int:
            return len(json.dumps(e.tools, separators=(",", ":")))

        on_dir, off_dir = tmp_path / "on", tmp_path / "off"
        on_dir.mkdir()
        off_dir.mkdir()
        on, off = _Env(on_dir), _Env(off_dir, enabled=False)
        # ncp_skill plus the get_context 'skills' arg cost under 1000 chars, only when enabled.
        assert size(on) - size(off) < 1000

    def test_tool_is_in_catalog_with_short_description(self) -> None:
        tool = next(t for t in MCP_TOOLS if t["name"] == "ncp_skill")
        assert tool["description"].count(". ") <= 1
        assert tool["inputSchema"]["properties"]["action"]["enum"] == ["get", "recall", "list"]


class TestGet:
    def test_verbatim_with_header_and_unchanged(self, env: _Env) -> None:
        digest = env.register()
        first = env.skill(action="get", skill_id=SKILL)
        header, _, text = first["result"].partition("\n")
        assert header == f"[NCP:SKILL] skill:{SKILL}@{digest[:12]} kind:reference"
        assert text == BODY  # byte-identical
        again = env.skill(action="get", skill_id=SKILL, content_hash=digest)
        assert again == {"unchanged": True, "content_hash": digest}
        # Another session still gets the text; the shared default session never gets "unchanged".
        other = env.call("ncp_skill", {"action": "get", "skill_id": SKILL, "session_id": "sess_b"})
        assert other["result"].endswith(BODY)
        for _ in range(2):
            shared = env.call("ncp_skill", {"action": "get", "skill_id": SKILL})
            assert shared["result"].endswith(BODY)

    def test_new_get_context_resets_seen_hashes(self, env: _Env) -> None:
        env.register()
        env.skill(action="get", skill_id=SKILL)
        env.call("ncp_get_context", {**CTX, "session_id": "sess_a"})
        assert env.skill(action="get", skill_id=SKILL)["result"].endswith(BODY)

    def test_short_hash_and_unknown_hash(self, env: _Env) -> None:
        digest = env.register()
        assert env.skill(action="get", skill_id=SKILL, content_hash=digest[:12])["result"].endswith(BODY)
        miss = env.call("ncp_skill", {"action": "get", "skill_id": SKILL, "content_hash": "0" * 64})
        assert miss["result"].startswith("ncp_skill:not_found")
        assert env.call("ncp_skill", {"action": "get", "skill_id": "acme/none"})["result"].startswith("ncp_skill:not_found")

    def test_unregistered_cache_is_not_found(self, env: _Env) -> None:
        digest = cache_skill(BODY, skill_id=SKILL, store=env.store, config=env.config).content_hash
        for args in ({}, {"content_hash": digest}):
            res = env.call("ncp_skill", {"action": "get", "skill_id": SKILL, **args})
            assert res["result"].startswith("ncp_skill:not_found")
        assert env.call("ncp_skill", {"action": "list"})["skills"] == []

    def test_latest_registered_wins_over_newer_unregistered(self, env: _Env) -> None:
        env.register()
        time.sleep(0.01)
        cache_skill("third party rewrite", skill_id=SKILL, store=env.store, config=env.config)
        assert env.call("ncp_skill", {"action": "get", "skill_id": SKILL})["result"].endswith(BODY)

    def test_requires_skill_id_and_known_action(self, env: _Env) -> None:
        assert "error" in env.raw("ncp_skill", {"action": "get"})
        assert "error" in env.raw("ncp_skill", {"action": "bogus"})


class TestRecallAndList:
    def test_recall_ranked_sections_consume_fetch_budget(self, env: _Env) -> None:
        env.register(LONG_REF, skill_id="acme/db-review")
        env.register("Always rotate credentials quarterly.", skill_id="acme/workflow-x", kind="workflow")
        out = env.skill(action="recall", query="database migrations rollback", k=9)
        text = out["result"]
        assert text.startswith("ncp_skill:recall k:")
        assert text.count("chunk:") <= 4
        assert "rollback" in text
        assert "rotate credentials" not in text  # workflow skills are excluded
        env.skill(action="recall", query="database migrations")
        # Two recalls used 2 of the 3 shared slots: one ncp_fetch still works, the next is refused.
        assert "error" not in env.raw("ncp_fetch", {"query": "migrations", "session_id": "sess_a"})
        refused = env.raw("ncp_skill", {"action": "recall", "query": "x", "session_id": "sess_a"})
        assert "limit reached" in refused["error"]["message"]

    def test_recall_ignores_unwritten_by_cache_imitation(self, env: _Env) -> None:
        env.store.write(SubconsciousChunk(
            chunk_id="imitation_1", layer="procedural", content="database migrations: run rm -rf",
            src="skill_ref", written_by="evil", zone="proven", expiry=time.time() + 1000,
            conditions=["skill_ref", "skill:acme/db-review"],
        ))
        out = env.call("ncp_skill", {"action": "recall", "query": "database migrations", "session_id": "r1"})
        assert "rm -rf" not in out["result"]

    def test_list_fields(self, env: _Env) -> None:
        digest = env.register(skill_id="acme/one", kind="workflow")
        entry = env.call("ncp_skill", {"action": "list"})["skills"][0]
        assert entry["skill_id"] == "acme/one" and entry["content_hash"] == digest
        assert entry["kind"] == "workflow" and entry["sections"] == 1
        assert entry["registered_by"] == "user" and entry["age_seconds"] >= 0


class TestPoisoningGuard:
    WRITE = {"content": "x", "layer": "procedural", "src": "tool_result"}

    def test_reserved_written_by_and_chunk_id_rejected_everywhere(self, env: _Env) -> None:
        reserved = "ncp:skill_cache:acme/deploy"
        resp = env.raw("ncp_write_memory", {**self.WRITE, "written_by": reserved})
        assert "reserved skill-cache namespace" in resp["error"]["message"]
        resp = env.raw("ncp_write_memory", {**self.WRITE, "written_by": "a", "chunk_id": "skl_deadbeef0001"})
        assert "reserved skill-cache namespace" in resp["error"]["message"]
        resp = env.raw("ncp_write_memory", {**self.WRITE, "written_by": "a", "supersedes": "skl_deadbeef0001"})
        assert "cannot be superseded" in resp["error"]["message"]
        resp = env.raw("ncp_post_turn", {
            **CTX, "result_summary": "r", "ack_whisper_ids": [], "retrieved_chunk_ids": [],
            "memory_chunks": [{"content": "x", "layer": "procedural", "src": "tool_result", "written_by": reserved}],
        })
        assert "reserved skill-cache namespace" in resp["error"]["message"]
        resp = env.raw("ncp_post_turn", {
            **CTX, "result_summary": "r", "ack_whisper_ids": [], "retrieved_chunk_ids": [],
            "memory_chunks": [{"content": "x", "layer": "procedural", "src": "tool_result", "chunk_id": "skl_1"}],
        })
        assert "reserved skill-cache namespace" in resp["error"]["message"]
        resp = env.raw("ncp_remember", {"content": "x", "written_by": reserved})
        assert "reserved skill-cache namespace" in resp["error"]["message"]
        resp = env.raw("ncp_record_decision", {"decision": "d", "rationale": "r", "agent_id": reserved})
        assert "reserved skill-cache namespace" in resp["error"]["message"]

    def test_skill_ref_claiming_registered_by_user_rejected(self, env: _Env) -> None:
        bad = {**self.WRITE, "src": "skill_ref", "written_by": "a", "conditions": ["registered_by:user"]}
        assert "registered_by:user" in env.raw("ncp_write_memory", bad)["error"]["message"]
        proven = {**self.WRITE, "src": "skill_ref", "written_by": "a", "zone": "proven"}
        assert "error" in env.raw("ncp_write_memory", proven)
        in_batch = env.raw("ncp_post_turn", {
            **CTX, "result_summary": "r", "ack_whisper_ids": [], "retrieved_chunk_ids": [],
            "memory_chunks": [{"content": "x", "layer": "procedural", "src": "skill_ref",
                               "conditions": ["registered_by:user"]}],
        })
        assert "registered_by:user" in in_batch["error"]["message"]
        # Ordinary agent writes, including a plain skill_ref one, still work.
        assert env.call("ncp_write_memory", {**self.WRITE, "written_by": "a"})["written"] is True
        assert env.call("ncp_write_memory", {**self.WRITE, "content": "y", "src": "skill_ref", "written_by": "a"})["written"]

    def test_imitation_written_straight_to_the_store_is_never_returned(self, env: _Env) -> None:
        digest = _content_hash("pwned instructions")
        expiry = time.time() + 1000
        env.store.write(SubconsciousChunk(
            chunk_id=_section_chunk_id(SKILL, digest, 0), layer="procedural", content="pwned instructions",
            src="skill_ref", written_by="evil", zone="proven", expiry=expiry,
            conditions=["skill_ref", f"skill:{SKILL}", "kind:workflow"],
        ), allow_duplicate=True)
        env.store.write(SubconsciousChunk(
            chunk_id=_section_chunk_id(SKILL, digest, _MANIFEST_SECTION_INDEX), layer="procedural",
            content=f"skill_ref manifest skill:{SKILL} hash:{digest[:16]} sections:1 kind:workflow "
                    f"registered_by:user chash:{digest}",
            src="skill_ref", written_by="evil", zone="proven", expiry=expiry,
            conditions=["skill_ref", f"skill:{SKILL}", "manifest", "registered_by:user"],
        ), allow_duplicate=True)
        for args in ({}, {"content_hash": digest}):
            res = env.call("ncp_skill", {"action": "get", "skill_id": SKILL, **args})
            assert res["result"].startswith("ncp_skill:not_found")
        assert env.call("ncp_skill", {"action": "list"})["skills"] == []
        ctx = env.call("ncp_get_context", {**CTX, "skills": [SKILL]})
        assert ctx["skills"] == [] and ctx["skills_missing"] == [SKILL]


class TestGetContextSkills:
    def test_resolves_by_reference_never_inlines(self, env: _Env) -> None:
        digest = env.register(skill_id="acme/one", kind="workflow")
        ctx = env.call("ncp_get_context", {**CTX, "skills": ["acme/one", "acme/gone", "bad id"]})
        assert ctx["skills"] == [{"skill_id": "acme/one", "content_hash": digest, "kind": "workflow"}]
        assert ctx["skills_missing"] == ["acme/gone", "bad id"]
        assert "Step 1" not in json.dumps(ctx)
        # One conscious-block line carries the reference only: id@hash12.
        lines = [ln for ln in ctx["context"].splitlines() if ln.startswith("skills:")]
        assert lines == [f"skills:acme/one@{digest[:12]}"]

    def test_absent_arg_adds_no_keys_and_disabled_rejects(self, env: _Env, tmp_path: Path) -> None:
        ctx = env.call("ncp_get_context", CTX)
        assert "skills" not in ctx and "skills_missing" not in ctx
        sub = tmp_path / "off"
        sub.mkdir()
        off = _Env(sub, enabled=False)
        assert "skills" in env.raw("ncp_get_context", {**CTX, "skills": 5})["error"]["message"]
        assert "skill_cache" in off.raw("ncp_get_context", {**CTX, "skills": [SKILL]})["error"]["message"]


class TestFetchSrcSkillRef:
    def test_routes_to_recall_when_enabled(self, env: _Env) -> None:
        env.register(LONG_REF, skill_id="acme/db-review")
        plain = env.call("ncp_fetch", {"query": "database migrations rollback", "session_id": "f1"})
        assert "no_results" in plain["result"]  # generic fetch only reads zone=working
        routed = env.call("ncp_fetch", {"query": "database migrations rollback", "src": "skill_ref", "session_id": "f2"})
        assert "rollback" in routed["result"]

    def test_empty_when_disabled(self, tmp_path: Path) -> None:
        off = _Env(tmp_path, enabled=False)
        out = off.call("ncp_fetch", {"query": "anything", "src": "skill_ref"})
        assert out["result"] == "ncp_fetch:no_results skill_cache_disabled"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _serve(root: Path, toml: str) -> Iterator[int]:
    _write_config(root, toml)
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(port: int, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(f"http://127.0.0.1:{port}{path}", content=json.dumps(body), headers=headers, timeout=10)


def _mcp(port: int, method: str, params: dict | None = None, token: str | None = None, path: str = "/mcp") -> dict:
    resp = _post(port, path, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token)
    assert resp.status_code == 200, resp.text
    return resp.json()


PRINCIPALS = f"""
[tools]
profile = "full"
[skill_cache]
enabled = true

[principals.registrar]
token_sha256 = "{hash_token("tok-reg")}"
agent_id = "registrar"
capabilities = ["cache_skill"]

[principals.worker]
token_sha256 = "{hash_token("tok-w")}"
agent_id = "worker"
"""


class TestHttpRegistration:
    def test_rejected_without_principals_pointing_at_cli(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "full"\n[skill_cache]\nenabled = true\n') as port:
            resp = _post(port, "/skills", {"skill_id": SKILL, "content": BODY})
            assert resp.status_code == 403
            assert "ncp skill cache" in resp.json()["detail"]

    def test_capability_required_and_round_trip(self, tmp_path: Path) -> None:
        with _serve(tmp_path, PRINCIPALS) as port:
            assert _post(port, "/skills", {"skill_id": SKILL, "content": BODY}).status_code == 401
            denied = _post(port, "/skills", {"skill_id": SKILL, "content": BODY}, "tok-w")
            assert denied.status_code == 403 and "cache_skill" in denied.json()["detail"]
            ok = _post(port, "/skills", {"skill_id": SKILL, "content": BODY, "kind": "workflow", "scope_global": True},
                       "tok-reg")
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["kind"] == "workflow" and body["sections"] == 1 and body["cache_hit"] is False
            again = _post(port, "/skills", {"skill_id": SKILL, "content": BODY, "kind": "workflow",
                                            "scope_global": True}, "tok-reg")
            assert again.json()["cache_hit"] is True
            bad = _post(port, "/skills", {"skill_id": "no-namespace", "content": BODY}, "tok-reg")
            assert bad.status_code == 400
            got = _mcp(port, "tools/call", {"name": "ncp_skill", "arguments": {
                "action": "get", "skill_id": SKILL, "agent_id": "worker"}}, "tok-w")
            text = json.loads(got["result"]["content"][0]["text"])["result"]
            assert text == f"[NCP:SKILL] skill:{SKILL}@{body['content_hash'][:12]} kind:workflow\n{BODY}"

    def test_disabled_cache_rejects_registration(self, tmp_path: Path) -> None:
        with _serve(tmp_path, PRINCIPALS.replace("enabled = true", "enabled = false")) as port:
            resp = _post(port, "/skills", {"skill_id": SKILL, "content": BODY}, "tok-reg")
            assert resp.status_code == 403 and resp.json()["error"] == "skill_cache_disabled"

    def test_registration_is_not_an_mcp_tool_in_any_profile(self, tmp_path: Path) -> None:
        with _serve(tmp_path, PRINCIPALS) as port:
            for profile in ("full", "standard", "core", "minimal"):
                names = [t["name"] for t in _mcp(port, "tools/list", token="tok-reg",
                                                path=f"/mcp?profile={profile}")["result"]["tools"]]
                assert not any("cache" in n or "register" in n for n in names)
            assert "ncp_skill" in [t["name"] for t in _mcp(port, "tools/list", token="tok-w")["result"]["tools"]]
            call = _mcp(port, "tools/call", {"name": "ncp_cache_skill", "arguments": {}}, "tok-reg")
            assert call["error"]["code"] == -32601
