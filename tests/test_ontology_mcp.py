"""Ontology PR 3: MCP tools, Python facade and permission gates.

Covers the generic MCP client path (tools/list + tools/call over stdio framing
and HTTP), byte-for-byte disabled-mode compatibility, the principal ->
AuthContext mapping, structured errors, ambiguity and limit clamping.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
import time
import socket
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from ncp.config import load_config
from ncp.mcp.server import (
    FORBIDDEN_ERROR_CODE,
    MCP_TOOLS,
    ONTOLOGY_MCP_TOOLS,
    ONTOLOGY_TOOL_NAMES,
    _handle_request,
    _read_message,
    create_http_server,
    make_handlers,
    serve_streams,
    tools_for_config,
)
from ncp.ontology.facade import (
    ERROR_CODES,
    ONTOLOGY_TOOL_NAMES as FACADE_TOOL_NAMES,
    OntologyError,
    OntologyFacade,
    auth_context_for,
)
from ncp.ontology.models import AuthContext, Scope
from ncp.ontology.policy import OntologyConfig
from ncp.principals import (
    IDENTITY_ARGS,
    LOOKUP_SCOPED_TOOLS,
    PIPELINE_SCOPED_TOOLS,
    AuthorizationError,
    Principal,
    ToolAuthorizer,
    hash_token,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

OBSERVED = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
ENABLED_TOML = '[ontology]\nenabled = true\n'


# ── helpers ──────────────────────────────────────────────────────────────────


def _project(tmp_path: Path, toml: str | None) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".ncp").mkdir()
    if toml is not None:
        (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")
    return root


def _frame(message: dict) -> bytes:
    payload = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


def _stdio(root: Path, *messages: dict) -> bytes:
    output = io.BytesIO()
    serve_streams(io.BytesIO(b"".join(_frame(m) for m in messages)), output, cwd=root)
    return output.getvalue()


def _responses(raw: bytes) -> list[dict]:
    stream = io.BytesIO(raw)
    found = []
    while (message := _read_message(stream)) is not None:
        found.append(message)
    return found


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def _call(name: str, arguments: dict, req_id: int = 1) -> dict:
    return _rpc("tools/call", {"name": name, "arguments": arguments}, req_id)


class InProcess:
    """Generic MCP client over the real JSON-RPC dispatcher (no transport)."""

    def __init__(self, root: Path, principal: Principal | None = None) -> None:
        self.config = load_config(cwd=root)
        self.store = SQLiteStore(root / ".ncp" / "store.db")
        self.tools = tools_for_config(self.config)
        self.handlers = make_handlers(self.store, config=self.config)
        self.principal = principal
        self.authorizer = ToolAuthorizer(self.store) if principal is not None else None

    def raw(self, name: str, arguments: dict) -> dict:
        return json.loads(_handle_request(  # type: ignore[arg-type]
            _call(name, arguments), self.handlers, tools=self.tools,
            principal=self.principal, authorizer=self.authorizer,
        ))

    def __call__(self, name: str, arguments: dict) -> dict:
        response = self.raw(name, arguments)
        assert "result" in response, response
        return json.loads(response["result"]["content"][0]["text"])


def service(key: str, *, aliases: list[str] | None = None, display: str | None = None) -> dict:
    return {
        "entity_type": "Service", "canonical_key": key, "display_name": display or key,
        "aliases": aliases or [], "attributes": {"name": key, "environment": "prod"},
    }


def database(key: str) -> dict:
    return {"entity_type": "Database", "canonical_key": key, "display_name": key,
            "attributes": {"name": key}}


def put_entity(client, pipeline: str, entity: dict) -> str:
    result = client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": pipeline, "entity": entity})
    assert result["ok"], result
    return result["entity"]["entity_id"]


def relation(src: str, dst: str, **extra: object) -> dict:
    return {"src_entity_id": src, "dst_entity_id": dst, "relation_type": "DEPENDS_ON",
            "confidence": 0.9, "observed_at": OBSERVED, **extra}


def attestation() -> dict:
    return {"evidence_type": "external_attestation", "evidence_id": "cmdb",
            "source_revision": "1", "observed_at": OBSERVED}


def put_relation(client, pipeline: str, key: str, body: dict) -> dict:
    return client("ncp_ontology_assert", {
        "kind": "relation", "pipeline_id": pipeline, "idempotency_key": key, "relation": body,
    })


def _error(result: dict, code: str) -> str:
    assert result["ok"] is False, result
    assert set(result) == {"ok", "error"}
    assert result["error"]["code"] == code, result
    return str(result["error"]["message"])


# ── disabled mode: byte-for-byte compatibility ───────────────────────────────


class TestDisabledCompatibility:
    def test_ontology_tools_are_not_in_the_legacy_catalog(self) -> None:
        assert not ONTOLOGY_TOOL_NAMES & {str(tool["name"]) for tool in MCP_TOOLS}
        assert ONTOLOGY_TOOL_NAMES == frozenset(FACADE_TOOL_NAMES)

    @pytest.mark.parametrize("toml", [
        None,
        "[ontology]\nenabled = false\n",
        '[tools]\nprofile = "standard"\n\n[ontology]\nenabled = false\nprovider = "builtin"\n',
    ])
    def test_tools_list_and_unknown_tool_bytes_unchanged(self, tmp_path: Path, toml: str | None) -> None:
        baseline_root = _project(tmp_path / "baseline", None)
        root = _project(tmp_path / "subject", toml)
        messages = (
            _rpc("tools/list", req_id=1),
            _call("ncp_ontology_query", {"key": "api"}, req_id=2),
            _call("ncp_ontology_capabilities", {}, req_id=3),
        )
        baseline = _stdio(baseline_root, *messages)
        assert _stdio(root, *messages) == baseline

        listed, query, caps = _responses(baseline)
        # Exactly the pre-ontology catalog, serialized the same way.
        expected = [tool for tool in MCP_TOOLS if tool in tools_for_config(load_config(cwd=baseline_root))]
        assert json.dumps(listed["result"]["tools"]) == json.dumps(expected)
        assert not ONTOLOGY_TOOL_NAMES & {tool["name"] for tool in listed["result"]["tools"]}
        # The ordinary unknown-tool error, as for any other unadvertised name.
        assert query["error"] == {"code": -32601, "message": "Tool not found: ncp_ontology_query"}
        assert caps["error"] == {"code": -32601, "message": "Tool not found: ncp_ontology_capabilities"}

    def test_disabled_builds_no_handlers_and_no_tables(self, tmp_path: Path) -> None:
        root = _project(tmp_path, "[ontology]\nenabled = false\n")
        client = InProcess(root)
        assert not ONTOLOGY_TOOL_NAMES & set(client.handlers)
        assert set(client.handlers) == set(make_handlers(client.store, config=None))
        with closing(sqlite3.connect(root / ".ncp" / "store.db")) as connection:
            tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith("ontology_") for name in tables)

    def test_core_profile_never_lists_ontology_tools(self, tmp_path: Path) -> None:
        root = _project(tmp_path, '[tools]\nprofile = "core"\n\n' + ENABLED_TOML)
        names = {tool["name"] for tool in tools_for_config(load_config(cwd=root))}
        assert not names & ONTOLOGY_TOOL_NAMES

    def test_existing_tool_response_unchanged_when_enabled_elsewhere(self, tmp_path: Path) -> None:
        # Enabling the ontology appends tools; the legacy entries stay identical
        # except that ncp_get_context advertises the optional ``ontology``
        # request extension (PR 4, design §6.1).
        off = tools_for_config(load_config(cwd=_project(tmp_path / "a", None)))
        on = tools_for_config(load_config(cwd=_project(tmp_path / "b", ENABLED_TOML)))
        legacy_view = []
        for tool in on[: len(off)]:
            if tool["name"] == "ncp_get_context":
                schema = dict(tool["inputSchema"])
                schema["properties"] = {
                    key: value for key, value in schema["properties"].items() if key != "ontology"
                }
                tool = {**tool, "inputSchema": schema}
            legacy_view.append(tool)
        assert json.dumps(legacy_view) == json.dumps(off)
        assert [tool["name"] for tool in on[len(off):]] == list(FACADE_TOOL_NAMES)


# ── enabled: generic MCP client over stdio framing ───────────────────────────


class TestStdioClient:
    def test_discover_assert_query_explain(self, tmp_path: Path) -> None:
        # verify lets the caller ask for unverified (candidate) edges (PR 4 policy).
        root = _project(tmp_path, ENABLED_TOML + 'unauthenticated_capabilities = ["read", "write", "verify"]\n')
        first = _stdio(root, _rpc("tools/list"), _call("ncp_ontology_capabilities", {}, req_id=2))
        listed, caps = _responses(first)
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert names[-len(FACADE_TOOL_NAMES):] == list(FACADE_TOOL_NAMES)
        capabilities = json.loads(caps["result"]["content"][0]["text"])
        assert capabilities["ok"] and capabilities["enabled"] and capabilities["version"] == "1"
        assert capabilities["provider"] == "builtin"
        assert capabilities["packs"][0]["id"] == "infrastructure"
        assert capabilities["packs"][0]["version"] == 1
        assert capabilities["packs"][0]["default"] is True
        assert [(p["version"], p["default"]) for p in capabilities["packs"]] == [(1, True), (2, False)]
        assert "DEPENDS_ON" in capabilities["packs"][0]["relation_types"]
        assert "Service" in capabilities["packs"][0]["entity_types"]
        assert capabilities["limits"]["max_hops"] == 2
        assert capabilities["retrieval_mode"] == "auto"
        assert capabilities["tools"] == list(FACADE_TOOL_NAMES)

        client = InProcess(root)
        api_id = put_entity(client, "p1", service("checkout", aliases=["co"]))
        db_id = put_entity(client, "p1", database("ledger"))
        asserted = put_relation(client, "p1", "k1", relation(api_id, db_id))
        assert asserted["ok"] and asserted["relation"]["created_by"] == "local"
        assert asserted["relation"]["status"] == "candidate"

        found = client("ncp_ontology_query", {"pipeline_id": "p1", "key": "co", "verified_only": False})
        assert found["ok"] and found["resolved"]
        assert found["seed"]["entity_id"] == api_id
        assert [path["nodes"] for path in found["paths"]] == [[api_id, db_id]]
        assert set(found["entities"]) == {api_id, db_id}
        assert "attributes" not in found["entities"][api_id]
        # Server default verified_only=true hides the candidate relation.
        assert client("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": api_id})["paths"] == []

        explained = client("ncp_ontology_explain", {
            "pipeline_id": "p1", "relation_id": asserted["relation"]["relation_id"],
        })
        assert explained["ok"] and explained["validity"]["active"] is True
        assert explained["validity"]["evidence_supported"] is False
        assert explained["source"]["entity_id"] == api_id

    def test_relation_replay_is_idempotent_and_mismatch_conflicts(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        api_id = put_entity(client, "p1", service("checkout"))
        db_id = put_entity(client, "p1", database("ledger"))
        first = put_relation(client, "p1", "same", relation(api_id, db_id))
        assert put_relation(client, "p1", "same", relation(api_id, db_id)) == first
        message = _error(put_relation(client, "p1", "same", relation(api_id, db_id, confidence=0.5)), "conflict")
        assert "idempotency_key" in message
        _error(put_relation(client, "p1", "", relation(api_id, db_id)), "invalid")

    def test_structured_errors(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        api_id = put_entity(client, "p1", service("checkout"))
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p1",
                                               "entity": {**service("x"), "scope": {"namespace": "p9"}}}), "invalid")
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p1",
                                               "entity": {**service("x"), "attributes": {}}}), "invalid")
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p1",
                                               "entity": service("checkout"), "expected_version": 0}), "conflict")
        _error(client("ncp_ontology_assert", {"kind": "entity", "entity": service("y")}), "invalid")
        _error(client("ncp_ontology_assert", {"kind": "edge", "pipeline_id": "p1"}), "invalid")
        _error(put_relation(client, "p1", "k", {**relation(api_id, api_id), "created_by": "mallory"}), "invalid")
        _error(put_relation(client, "p1", "k", relation(api_id, "00000000-0000-0000-0000-000000000000")),
               "not_found")
        _error(client("ncp_ontology_explain", {"relation_id": "not-a-uuid"}), "invalid")
        _error(client("ncp_ontology_query", {"pipeline_id": "p1", "key": "nothing"}), "not_found")
        _error(client("ncp_ontology_query", {"pipeline_id": "p1"}), "invalid")
        _error(client("ncp_ontology_query", {"pipeline_id": "p1", "key": "checkout",
                                              "relation_types": ["X"] * 101}), "limit")
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p1", "entity": {
            **service("big"), "attributes": {"name": "big", "environment": "x" * 20_000}}}), "limit")
        relation_id = put_relation(client, "p1", "k2", relation(api_id, put_entity(
            client, "p1", database("ledger"))))["relation"]["relation_id"]
        _error(client("ncp_ontology_revoke", {"pipeline_id": "p1", "relation_id": relation_id, "reason": " "}),
               "invalid")
        revoked = client("ncp_ontology_revoke", {"pipeline_id": "p1", "relation_id": relation_id, "reason": "gone"})
        assert revoked["ok"] and revoked["relation"]["status"] == "revoked"
        assert client("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": relation_id})[
            "validity"]["active"] is False

    def test_unauthenticated_defaults_deny_verify_and_global_writes(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        api_id = put_entity(client, "p1", service("checkout"))
        db_id = put_entity(client, "p1", database("ledger"))
        message = _error(put_relation(client, "p1", "v", relation(
            api_id, db_id, status="verified", evidence=[attestation()])), "forbidden")
        assert "verify" in message
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "global",
                                               "entity": database("shared")}), "forbidden")

    def test_unauthenticated_capabilities_are_configurable(self, tmp_path: Path) -> None:
        client = InProcess(_project(
            tmp_path, ENABLED_TOML + 'unauthenticated_capabilities = ["read", "write", "verify", "admin"]\n',
        ))
        api_id = put_entity(client, "p1", service("checkout"))
        db_id = put_entity(client, "global", database("shared"))
        verified = put_relation(client, "p1", "v", relation(api_id, db_id, status="verified",
                                                              evidence=[attestation()]))
        assert verified["ok"] and verified["relation"]["status"] == "verified"
        found = client("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": api_id})
        assert [path["nodes"] for path in found["paths"]] == [[api_id, db_id]]
        assert found["entities"][db_id]["namespace"] == "global"

    def test_read_only_unauthenticated_cannot_write(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML + 'unauthenticated_capabilities = ["read"]\n'))
        _error(client("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p1",
                                               "entity": service("checkout")}), "forbidden")

    def test_invalid_unauthenticated_capabilities_fail_config_load(self, tmp_path: Path) -> None:
        for bad in ('["root"]', '["read", "read"]', '"read"'):
            root = _project(tmp_path / bad.replace('"', "").replace(" ", ""),
                            ENABLED_TOML + f"unauthenticated_capabilities = {bad}\n")
            with pytest.raises(ValueError):
                load_config(cwd=root)


# ── ambiguity, limits, evidence liveness ─────────────────────────────────────


class TestQuerySemantics:
    def test_ambiguous_alias_returns_candidates_never_a_guess(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        first = put_entity(client, "p1", service("payments-api", aliases=["api"]))
        second = put_entity(client, "p1", service("search-api", aliases=["api"]))
        result = client("ncp_ontology_query", {"pipeline_id": "p1", "key": "API"})
        assert result["ok"] and result["resolved"] is False
        assert result["paths"] == [] and result["entities"] == {}
        ambiguity = result["ambiguity"]
        assert ambiguity["matched_key"] == "api" and ambiguity["matched_on"] == "alias"
        assert sorted(item["entity_id"] for item in ambiguity["candidates"]) == sorted([first, second])
        # An exact canonical key beats any alias.
        put_entity(client, "p1", service("api"))
        exact = client("ncp_ontology_query", {"pipeline_id": "p1", "key": "api"})
        assert exact["resolved"] and exact["seed"]["canonical_key"] == "api"
        # entity_type narrows lookup; a type with no match is not_found.
        _error(client("ncp_ontology_query", {"pipeline_id": "p1", "key": "api", "entity_type": "Team"}),
               "not_found")

    def test_same_key_in_pipeline_and_global_is_ambiguous(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML + 'unauthenticated_capabilities = ["read", "write", "admin"]\n'))
        put_entity(client, "p1", database("ledger"))
        put_entity(client, "global", database("ledger"))
        result = client("ncp_ontology_query", {"pipeline_id": "p1", "key": "ledger"})
        assert result["resolved"] is False
        assert {item["namespace"] for item in result["ambiguity"]["candidates"]} == {"p1", "global"}
        # From another pipeline only the global one is visible: resolved.
        other = client("ncp_ontology_query", {"pipeline_id": "p2", "key": "ledger"})
        assert other["resolved"] and other["seed"]["namespace"] == "global"

    def test_limits_are_clamped_to_config(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML + "max_hops = 1\nmax_nodes = 3\nmax_edges = 2\n"
                                    'unauthenticated_capabilities = ["read", "write", "verify"]\n'))
        hub = put_entity(client, "p1", service("hub"))
        spokes = [put_entity(client, "p1", database(f"db{i}")) for i in range(4)]
        for index, spoke in enumerate(spokes):
            put_relation(client, "p1", f"k{index}", relation(hub, spoke))
        result = client("ncp_ontology_query", {
            "pipeline_id": "p1", "entity_id": hub, "verified_only": False,
            "max_hops": 5, "max_nodes": 100, "max_edges": 200,
        })
        assert result["limits"] == {"max_hops": 1, "max_nodes": 3, "max_edges": 2, "verified_only": False}
        assert {"clamped: max_hops to 1", "clamped: max_nodes to 3", "clamped: max_edges to 2"} <= set(
            result["warnings"])
        assert len(result["paths"]) == 2
        assert any(item.startswith("truncated:") for item in result["warnings"])
        smaller = client("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": hub,
                                                 "verified_only": False, "max_edges": 1})
        assert len(smaller["paths"]) == 1 and smaller["limits"]["max_edges"] == 1
        for bad in ({"max_hops": -1}, {"max_nodes": 0}, {"max_edges": "5"}, {"max_hops": True},
                    {"verified_only": "yes"}):
            _error(client("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": hub, **bad}), "invalid")

    def test_explain_reports_invalidated_chunk_evidence(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ENABLED_TOML + 'unauthenticated_capabilities = ["read", "write", "verify"]\n')
        client = InProcess(root)
        chunk = SubconsciousChunk(layer="semantic", content="checkout depends on ledger",
                                  src="tool_result", pipeline_id="p1")
        assert client.store.write(chunk)
        api_id = put_entity(client, "p1", service("checkout"))
        db_id = put_entity(client, "p1", database("ledger"))
        evidence = {"evidence_type": "chunk", "evidence_id": chunk.chunk_id, "source_revision": "1",
                    "observed_at": OBSERVED}
        asserted = put_relation(client, "p1", "v", relation(api_id, db_id, status="verified", evidence=[evidence]))
        relation_id = asserted["relation"]["relation_id"]
        before = client("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": relation_id})
        assert before["validity"]["verified_and_supported"] is True
        assert before["evidence"][0]["live"] is True

        facade = OntologyFacade.from_config(client.config, client.store)
        assert facade.store is not None
        auth = AuthContext(principal_id="local", scope=Scope(tenant_id="default", namespace="p1"),
                           capabilities=frozenset({"read", "write", "verify"}))
        assert facade.store.invalidate_evidence([chunk.chunk_id], auth=auth, reason="stale") == 1
        after = client("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": relation_id})
        assert after["validity"]["status"] == "candidate"
        assert after["validity"]["verified_and_supported"] is False
        assert after["evidence"][0]["status"] == "invalidated" and after["evidence"][0]["live"] is False
        assert after["evidence"][0]["invalidated_reason"] == "stale"


# ── facade unit tests ────────────────────────────────────────────────────────


class TestFacade:
    def test_disabled_capabilities_and_errors(self, tmp_path: Path) -> None:
        facade = OntologyFacade.from_config(None, None)
        assert facade.capabilities() == {"enabled": False, "version": "1"}
        assert OntologyFacade.from_config(OntologyConfig(), None).enabled is False
        assert OntologyFacade.from_config(
            OntologyConfig(enabled=True, provider="disabled"), None).enabled is False
        auth = AuthContext(principal_id="x", scope=Scope(tenant_id="default", namespace="p1"),
                           capabilities=frozenset({"read"}))
        for operation in (
            lambda: facade.query(auth, key="api"),
            lambda: facade.explain(auth, "00000000-0000-0000-0000-000000000000"),
            lambda: facade.assert_entity(auth, service("api")),
            lambda: facade.assert_relation(auth, {}, idempotency_key="k"),
            lambda: facade.revoke_relation(auth, "00000000-0000-0000-0000-000000000000", reason="r"),
        ):
            with pytest.raises(OntologyError) as caught:
                operation()
            assert caught.value.code == "disabled"
            assert caught.value.to_result()["error"]["code"] == "disabled"

    def test_non_sqlite_store_is_disabled_with_reason(self) -> None:
        facade = OntologyFacade.from_config(OntologyConfig(enabled=True), object())
        assert facade.capabilities() == {"enabled": False, "version": "1"}
        assert facade.disabled_reason is not None and "object" in facade.disabled_reason

    def test_unknown_provider_or_pack_fails_closed(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        with pytest.raises(ValueError, match="provider"):
            OntologyFacade.from_config(OntologyConfig(enabled=True, provider="neo4j"), store)
        with pytest.raises(ValueError, match="packs"):
            OntologyFacade.from_config(OntologyConfig(enabled=True, packs=("nope",)), store)

    def test_disabled_tool_handlers_return_disabled_errors(self) -> None:
        from ncp.ontology.facade import make_tool_handlers

        handlers = make_tool_handlers(OntologyFacade.disabled(), lambda: None)
        assert handlers["ncp_ontology_capabilities"]({}) == {"ok": True, "enabled": False, "version": "1"}
        for name in FACADE_TOOL_NAMES[1:]:
            _error(handlers[name]({"kind": "bogus"}), "disabled")

    def test_error_codes_are_closed(self) -> None:
        assert ERROR_CODES == {"conflict", "forbidden", "invalid", "not_found", "disabled", "limit"}
        with pytest.raises(ValueError):
            OntologyError("teapot", "no")

    def test_python_facade_in_process(self, tmp_path: Path) -> None:
        facade = OntologyFacade.from_config(
            OntologyConfig(enabled=True, unauthenticated_capabilities=("read", "write", "verify")),
            SQLiteStore(tmp_path / "s.db"),
        )
        auth = auth_context_for(None, "p1", config=facade.config)
        api = facade.assert_entity(auth, service("checkout"))["entity"]["entity_id"]
        db = facade.assert_entity(auth, database("ledger"))["entity"]["entity_id"]
        rel = facade.assert_relation(auth, relation(api, db), idempotency_key="k")
        assert facade.query(auth, entity_id=api, verified_only=False)["paths"][0]["nodes"] == [api, db]
        assert facade.explain(auth, rel["relation"]["relation_id"])["ok"]
        other = auth_context_for(None, "p2", config=facade.config)
        with pytest.raises(OntologyError) as caught:
            facade.explain(other, rel["relation"]["relation_id"])
        assert caught.value.code == "not_found"


# ── principal -> AuthContext mapping ─────────────────────────────────────────


def _principal(name: str = "alice", *, pipelines: set[str] | None = None, caps: set[str] | None = None) -> Principal:
    return Principal(name=name, agent_id=name, pipelines=None if pipelines is None else frozenset(pipelines),
                     capabilities=frozenset(caps or ()))


class TestAuthMapping:
    config = OntologyConfig(enabled=True)

    def test_no_principal_uses_unauthenticated_capabilities(self) -> None:
        auth = auth_context_for(None, "p1", config=self.config)
        assert auth.principal_id == "local" and auth.capabilities == {"read", "write"}

    @pytest.mark.parametrize(("principal", "namespace", "expected"), [
        (_principal(), "p1", {"read", "write"}),
        (_principal(), "global", {"read"}),
        (_principal(pipelines={"p1"}), "p1", {"read", "write"}),
        (_principal(pipelines={"p1"}), "global", {"read"}),
        (_principal(caps={"verify"}), "p1", {"read", "write", "verify"}),
        (_principal(caps={"admin"}), "global", {"read", "write", "admin"}),
        (_principal(caps={"record_outcome"}), "p1", {"read", "write"}),
    ])
    def test_capability_mapping(self, principal: Principal, namespace: str, expected: set[str]) -> None:
        auth = auth_context_for(principal, namespace, config=self.config)
        assert auth.capabilities == expected
        assert auth.principal_id == principal.name
        assert auth.scope == Scope(tenant_id="default", namespace=namespace)

    def test_pipeline_outside_allowlist_is_forbidden(self) -> None:
        with pytest.raises(OntologyError) as caught:
            auth_context_for(_principal(pipelines={"p1"}), "p2", config=self.config)
        assert caught.value.code == "forbidden"

    def test_invalid_namespace_is_invalid(self) -> None:
        with pytest.raises(OntologyError) as caught:
            auth_context_for(None, "   ", config=self.config)
        assert caught.value.code == "invalid"

    def test_tools_are_classified_for_authorization(self) -> None:
        classified = set(IDENTITY_ARGS) | PIPELINE_SCOPED_TOOLS | LOOKUP_SCOPED_TOOLS
        for tool in ONTOLOGY_MCP_TOOLS:
            name = str(tool["name"])
            props = tool["inputSchema"]["properties"]  # type: ignore[index]
            assert name in classified
            if "pipeline_id" in props:
                assert name in PIPELINE_SCOPED_TOOLS
            assert not {"agent_id", "written_by", "from", "created_by", "principal_id"} & set(props)

    def test_authorizer_enforces_allowlist_but_allows_global(self, tmp_path: Path) -> None:
        authorizer = ToolAuthorizer(SQLiteStore(tmp_path / "s.db"))
        limited = _principal(pipelines={"p1"})
        authorizer.check_call(limited, "ncp_ontology_query", {"pipeline_id": "p1"})
        authorizer.check_call(limited, "ncp_ontology_query", {"pipeline_id": "global"})
        authorizer.check_call(limited, "ncp_ontology_capabilities", {})
        with pytest.raises(AuthorizationError, match="may not access pipeline 'p2'"):
            authorizer.check_call(limited, "ncp_ontology_explain", {"pipeline_id": "p2", "relation_id": "x"})
        with pytest.raises(AuthorizationError, match="pipeline_id is required"):
            authorizer.check_call(limited, "ncp_ontology_query", {"key": "api"})

    def test_explicit_relation_id_cannot_probe_or_squat_other_scopes(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ENABLED_TOML)
        alice = InProcess(root, principal=_principal("alice", pipelines={"p1"}))
        bob = InProcess(root, principal=_principal("bob", pipelines={"p2"}))
        a_src, a_dst = put_entity(alice, "p1", service("checkout")), put_entity(alice, "p1", database("ledger"))
        b_src, b_dst = put_entity(bob, "p2", service("search")), put_entity(bob, "p2", database("index"))
        hidden = put_relation(alice, "p1", "deploy-1", relation(a_src, a_dst))["relation"]["relation_id"]
        # An id that exists in p1 and one that exists nowhere look the same from p2.
        probe_hidden = put_relation(bob, "p2", "probe-1", relation(b_src, b_dst, relation_id=hidden))
        probe_absent = put_relation(bob, "p2", "probe-2", relation(
            b_src, b_dst, relation_id="00000000-0000-0000-0000-000000000001"))
        assert probe_hidden == probe_absent
        _error(probe_hidden, "not_found")
        # p2 cannot pre-claim the id p1 derives from its idempotency key.
        own = put_relation(bob, "p2", "deploy-2", relation(b_src, b_dst))
        assert own["ok"]
        derived_p1 = put_relation(alice, "p1", "deploy-2", relation(a_src, a_dst))
        assert derived_p1["ok"] and derived_p1["relation"]["relation_id"] != own["relation"]["relation_id"]
        # Re-asserting an existing relation of the caller's own scope by id still works.
        again = put_relation(alice, "p1", "deploy-3", relation(a_src, a_dst, relation_id=hidden, confidence=0.5))
        assert again["ok"] and again["relation"]["revision"] == 2

    def test_principal_comes_from_dispatcher_not_arguments(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ENABLED_TOML)
        verifier = InProcess(root, principal=_principal("checker", caps={"verify"}))
        api_id = put_entity(verifier, "p1", service("checkout"))
        db_id = put_entity(verifier, "p1", database("ledger"))
        verified = put_relation(verifier, "p1", "v", relation(api_id, db_id, status="verified",
                                                              evidence=[attestation()]))
        assert verified["ok"] and verified["relation"]["created_by"] == "checker"
        # The same server state, no principal: smuggled identity arguments are ignored.
        anonymous = InProcess(root)
        result = anonymous("ncp_ontology_assert", {
            "kind": "relation", "pipeline_id": "p1", "idempotency_key": "w", "principal_id": "checker",
            "capabilities": ["verify"], "relation": relation(api_id, db_id, status="verified",
                                                              evidence=[attestation()]),
        })
        _error(result, "forbidden")


# ── HTTP transport with [principals] ─────────────────────────────────────────

TOKENS = {"alice": "tok-alice", "bob": "tok-bob", "router": "tok-router"}
PRINCIPALS_TOML = ENABLED_TOML + f"""
[principals.alice]
token_sha256 = "{hash_token(TOKENS["alice"])}"
agent_id = "alice"

[principals.bob]
token_sha256 = "{hash_token(TOKENS["bob"])}"
agent_id = "bob"
pipelines = ["p1"]

[principals.router]
token_sha256 = "{hash_token(TOKENS["router"])}"
agent_id = "router"
capabilities = ["admin", "verify"]
"""


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HttpClient:
    def __init__(self, port: int) -> None:
        self.port = port
        self._ids = 0

    def raw(self, name: str, arguments: dict, *, as_: str | None) -> dict:
        self._ids += 1
        headers = {"Content-Type": "application/json"}
        if as_ is not None:
            headers["Authorization"] = f"Bearer {TOKENS[as_]}"
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0) as client:
            response = client.post("/mcp", content=json.dumps(_call(name, arguments, self._ids)), headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def __call__(self, name: str, arguments: dict, *, as_: str | None) -> dict:
        payload = self.raw(name, arguments, as_=as_)
        assert "result" in payload, payload
        return json.loads(payload["result"]["content"][0]["text"])

    def bound(self, as_: str | None):  # noqa: ANN201 - adapter for put_entity/put_relation
        return lambda name, arguments: self(name, arguments, as_=as_)


@contextmanager
def _http(root: Path) -> Iterator[HttpClient]:
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpClient(port)
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http(tmp_path: Path) -> Iterator[HttpClient]:
    with _http(_project(tmp_path, PRINCIPALS_TOML)) as client:
        yield client


class TestHttpPrincipals:
    def test_tools_list_includes_ontology_tools(self, http: HttpClient) -> None:
        with httpx.Client(base_url=f"http://127.0.0.1:{http.port}", timeout=10.0) as client:
            response = client.post("/mcp", content=json.dumps(_rpc("tools/list")), headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {TOKENS['bob']}"})
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert ONTOLOGY_TOOL_NAMES <= names

    def test_pipeline_allowlist_is_enforced(self, http: HttpClient) -> None:
        put_entity(http.bound("alice"), "p2", service("checkout"))
        denied = http.raw("ncp_ontology_query", {"pipeline_id": "p2", "key": "checkout"}, as_="bob")
        assert denied["error"]["code"] == FORBIDDEN_ERROR_CODE
        required = http.raw("ncp_ontology_query", {"key": "checkout"}, as_="bob")
        assert required["error"]["code"] == FORBIDDEN_ERROR_CODE
        assert "pipeline_id is required" in required["error"]["message"]
        denied_write = http.raw("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "p2",
                                                         "entity": service("x")}, as_="bob")
        assert denied_write["error"]["code"] == FORBIDDEN_ERROR_CODE
        assert put_entity(http.bound("bob"), "p1", service("checkout"))

    def test_global_is_readable_by_all_and_writable_by_admin_only(self, http: HttpClient) -> None:
        _error(http("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "global",
                                             "entity": database("shared")}, as_="alice"), "forbidden")
        _error(http("ncp_ontology_assert", {"kind": "entity", "pipeline_id": "global",
                                             "entity": database("shared")}, as_="bob"), "forbidden")
        shared = put_entity(http.bound("router"), "global", database("shared"))
        for who, pipeline in (("bob", "global"), ("bob", "p1"), ("alice", "p7")):
            found = http("ncp_ontology_query", {"pipeline_id": pipeline, "key": "shared"}, as_=who)
            assert found["resolved"] and found["seed"]["entity_id"] == shared

    def test_verify_capability_gates_verified_relations(self, http: HttpClient) -> None:
        alice = http.bound("alice")
        api_id = put_entity(alice, "p1", service("checkout"))
        db_id = put_entity(alice, "p1", database("ledger"))
        body = relation(api_id, db_id, status="verified", evidence=[attestation()])
        _error(put_relation(alice, "p1", "v", body), "forbidden")
        verified = put_relation(http.bound("router"), "p1", "v", body)
        assert verified["ok"] and verified["relation"]["created_by"] == "router"
        assert http("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": api_id}, as_="alice")["paths"]

    def test_revoking_another_principals_relation_needs_admin(self, http: HttpClient) -> None:
        alice = http.bound("alice")
        api_id = put_entity(alice, "p1", service("checkout"))
        db_id = put_entity(alice, "p1", database("ledger"))
        relation_id = put_relation(alice, "p1", "k", relation(api_id, db_id))["relation"]["relation_id"]
        args = {"pipeline_id": "p1", "relation_id": relation_id, "reason": "wrong"}
        _error(http("ncp_ontology_revoke", args, as_="bob"), "forbidden")
        assert http("ncp_ontology_revoke", args, as_="router")["relation"]["status"] == "revoked"

    def test_cross_pipeline_reads_are_not_found(self, http: HttpClient) -> None:
        alice = http.bound("alice")
        api_id = put_entity(alice, "p2", service("checkout"))
        db_id = put_entity(alice, "p2", database("ledger"))
        relation_id = put_relation(alice, "p2", "k", relation(api_id, db_id))["relation"]["relation_id"]
        missing = "00000000-0000-0000-0000-000000000000"
        hidden = http("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": relation_id}, as_="bob")
        absent = http("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": missing}, as_="bob")
        assert hidden == absent  # indistinguishable from a missing relation
        _error(hidden, "not_found")
        _error(http("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": api_id}, as_="bob"), "not_found")
        _error(http("ncp_ontology_query", {"pipeline_id": "p1", "key": "checkout"}, as_="bob"), "not_found")
        _error(http("ncp_ontology_revoke", {"pipeline_id": "p1", "relation_id": relation_id, "reason": "x"},
                    as_="router"), "not_found")
        # Relating to another pipeline's entity fails as not_found too.
        own = put_entity(http.bound("bob"), "p1", service("search"))
        _error(put_relation(http.bound("bob"), "p1", "x", relation(own, db_id)), "not_found")

    def test_http_without_principals_uses_unauthenticated_capabilities(self, tmp_path: Path) -> None:
        with _http(_project(tmp_path, ENABLED_TOML)) as client:
            anonymous = client.bound(None)
            api_id = put_entity(anonymous, "p1", service("checkout"))
            db_id = put_entity(anonymous, "p1", database("ledger"))
            created = put_relation(anonymous, "p1", "k", relation(api_id, db_id))
            assert created["relation"]["created_by"] == "local"
            _error(put_relation(anonymous, "p1", "v", relation(api_id, db_id, status="verified",
                                                                evidence=[attestation()])), "forbidden")


# ── review follow-ups: exact-id evidence and as_of on read tools ─────────────


def _memory(store: SQLiteStore, content: str, pipeline: str | None, **extra: object) -> str:
    chunk = SubconsciousChunk(layer="semantic", content=content, src="tool_result",
                              pipeline_id=pipeline, **extra)
    assert store.write(chunk)
    return chunk.chunk_id


class TestEvidenceTool:
    def test_listed_only_when_enabled(self, tmp_path: Path) -> None:
        enabled = tools_for_config(load_config(cwd=_project(tmp_path / "on", ENABLED_TOML)))
        disabled = tools_for_config(load_config(cwd=_project(tmp_path / "off", None)))
        assert "ncp_ontology_evidence" in {tool["name"] for tool in enabled}
        assert "ncp_ontology_evidence" not in {tool["name"] for tool in disabled}
        assert "ncp_ontology_evidence" in FACADE_TOOL_NAMES

    def test_returns_live_readable_text_and_hides_the_rest(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ENABLED_TOML)
        alice = InProcess(root, principal=_principal("alice", pipelines={"p1"}))
        bob = InProcess(root, principal=_principal("bob", pipelines={"p2"}))
        mine = _memory(alice.store, "checkout writes the ledger", "p1")
        shared = _memory(alice.store, "global runbook", None, scope="global")
        secret = _memory(alice.store, "p2 secret", "p2")
        expired = _memory(alice.store, "old", "p1", expiry=time.time() - 60)
        ids = [mine, shared, secret, expired, "sub_nope", mine]
        result = alice("ncp_ontology_evidence", {"pipeline_id": "p1", "chunk_ids": ids})
        assert result["ok"]
        assert [c["chunk_id"] for c in result["chunks"]] == [mine, shared]
        assert result["chunks"][0]["content"] == "checkout writes the ledger"
        # Unknown, expired and out-of-scope ids are indistinguishable.
        assert result["missing"] == [secret, expired, "sub_nope"]
        from_bob = bob("ncp_ontology_evidence", {"pipeline_id": "p2", "chunk_ids": ids})
        assert [c["chunk_id"] for c in from_bob["chunks"]] == [shared, secret]
        assert from_bob["missing"] == [mine, expired, "sub_nope"]
        # global reads only global-scope chunks; another pipeline is refused.
        glob = bob("ncp_ontology_evidence", {"pipeline_id": "global", "chunk_ids": ids})
        assert [c["chunk_id"] for c in glob["chunks"]] == [shared]
        denied = bob.raw("ncp_ontology_evidence", {"pipeline_id": "p1", "chunk_ids": [mine]})
        assert denied["error"]["code"] == -32003 and "may not access pipeline 'p1'" in denied[
            "error"]["message"]

    def test_bounds_and_input_errors(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        _error(client("ncp_ontology_evidence", {"pipeline_id": "p1", "chunk_ids": []}), "invalid")
        _error(client("ncp_ontology_evidence", {"pipeline_id": "p1", "chunk_ids": "sub_x"}), "invalid")
        _error(client("ncp_ontology_evidence", {"pipeline_id": "p1", "chunk_ids": [""]}), "invalid")
        _error(client("ncp_ontology_evidence", {
            "pipeline_id": "p1", "chunk_ids": [f"sub_{index}" for index in range(17)],
        }), "limit")
        sixteen = client("ncp_ontology_evidence", {
            "pipeline_id": "p1", "chunk_ids": [f"sub_{index}" for index in range(16)],
        })
        assert sixteen["ok"] and sixteen["chunks"] == [] and len(sixteen["missing"]) == 16

    def test_oversized_array_is_rejected_without_a_full_scan(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        started = time.perf_counter()
        _error(client("ncp_ontology_evidence", {
            "pipeline_id": "p1", "chunk_ids": [f"sub_{index}" for index in range(200_000)],
        }), "limit")
        assert time.perf_counter() - started < 5.0  # was quadratic: minutes
        # Duplicates do not count toward the limit (dedupe keeps first order).
        repeated = client("ncp_ontology_evidence", {
            "pipeline_id": "p1", "chunk_ids": ["sub_b", " sub_a "] * 5_000,
        })
        assert repeated["ok"] and repeated["missing"] == ["sub_b", "sub_a"]

    def test_python_facade_method(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ENABLED_TOML)
        store = SQLiteStore(root / ".ncp" / "store.db")
        facade = OntologyFacade.from_config(load_config(cwd=root), store)
        chunk_id = _memory(store, "api uses orders", "p1")
        auth = auth_context_for(None, "p1", config=facade.config)
        assert facade.evidence(auth, [chunk_id, "sub_x"]) == {
            "ok": True, "missing": ["sub_x"],
            "chunks": [{"chunk_id": chunk_id, "layer": "semantic", "src": "tool_result",
                        "content": "api uses orders"}],
        }
        with pytest.raises(OntologyError) as excinfo:
            OntologyFacade.disabled().evidence(auth, [chunk_id])
        assert excinfo.value.code == "disabled"

    def test_read_tools_reject_as_of(self, tmp_path: Path) -> None:
        client = InProcess(_project(tmp_path, ENABLED_TOML))
        api = put_entity(client, "p1", service("checkout"))
        as_of = "2020-01-01T00:00:00Z"
        for name, args in (
            ("ncp_ontology_query", {"entity_id": api}),
            ("ncp_ontology_explain", {"relation_id": "00000000-0000-0000-0000-000000000001"}),
            ("ncp_ontology_evidence", {"chunk_ids": ["sub_x"]}),
        ):
            message = _error(client(name, {"pipeline_id": "p1", "as_of": as_of, **args}), "invalid")
            assert "as_of" in message and "current" in message
        assert client("ncp_ontology_query", {"pipeline_id": "p1", "entity_id": api})["ok"]
