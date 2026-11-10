"""Unit tests for ncp.principals: config parsing, token resolution, authorization."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.config import load_config
from ncp.mcp.server import MCP_TOOLS
from ncp.principals import (
    CAPABILITIES,
    IDENTITY_ARGS,
    LEGACY_PRINCIPAL_NAME,
    LOOKUP_SCOPED_TOOLS,
    PIPELINE_SCOPED_TOOLS,
    AuthorizationError,
    Principal,
    PrincipalConfigError,
    ToolAuthorizer,
    authorize_events,
    authorize_read,
    hash_token,
    load_principals,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _p(
    agent_id: str | None = "alice",
    *,
    pipelines: set[str] | None = None,
    caps: set[str] | None = None,
    name: str = "alice",
) -> Principal:
    return Principal(
        name=name,
        agent_id=agent_id,
        pipelines=None if pipelines is None else frozenset(pipelines),
        capabilities=frozenset(caps or set()),
    )


# ── config parsing ───────────────────────────────────────────────────────────


class TestLoadPrincipals:
    def test_absent_or_empty_means_off(self) -> None:
        assert load_principals(None) is None
        assert load_principals({}) is None

    def test_hash_and_env_tokens_resolve(self) -> None:
        registry = load_principals(
            {
                "alice": {"agent_id": "alice", "token_sha256": hash_token("tok-a")},
                "bob": {
                    "agent_id": "bob",
                    "token_env": "BOB_TOKEN",
                    "pipelines": ["p1"],
                    "capabilities": ["verify", "record_outcome"],
                },
            },
            env={"BOB_TOKEN": "tok-b"},
        )
        assert registry is not None
        alice = registry.resolve_token("tok-a")
        bob = registry.resolve_authorization("Bearer tok-b")
        assert alice is not None and alice.agent_id == "alice" and alice.pipelines is None
        assert alice.capabilities == frozenset()
        assert bob is not None and bob.pipelines == frozenset({"p1"})
        assert bob.capabilities == frozenset({"verify", "record_outcome"})
        assert registry.resolve_token("nope") is None
        assert registry.resolve_token("") is None
        assert registry.resolve_authorization("Basic tok-a") is None
        assert registry.resolve_authorization(None) is None

    def test_uppercase_hash_is_accepted(self) -> None:
        registry = load_principals({"a": {"agent_id": "a", "token_sha256": hash_token("t").upper()}})
        assert registry is not None and registry.resolve_token("t") is not None

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            ({"agent_id": "a", "token": "plain"}, "plaintext"),
            ({"agent_id": "a"}, "exactly one"),
            ({"agent_id": "a", "token_sha256": hash_token("x"), "token_env": "X"}, "exactly one"),
            ({"agent_id": "a", "token_sha256": "abc"}, "64 hex"),
            ({"token_sha256": hash_token("x")}, "agent_id"),
            ({"agent_id": "a b", "token_sha256": hash_token("x")}, "agent_id"),
            ({"agent_id": "a", "token_env": "MISSING_VAR"}, "unset or empty"),
            ({"agent_id": "a", "token_sha256": hash_token("x"), "pipelines": []}, "empty"),
            ({"agent_id": "a", "token_sha256": hash_token("x"), "pipelines": "p1"}, "array"),
            ({"agent_id": "a", "token_sha256": hash_token("x"), "capabilities": ["root"]}, "unknown values"),
            ({"agent_id": "a", "token_sha256": hash_token("x"), "agentid": "typo"}, "unknown keys"),
        ],
    )
    def test_invalid_entries_are_rejected(self, entry: dict, message: str) -> None:
        with pytest.raises(PrincipalConfigError, match=message):
            load_principals({"a": entry}, env={})

    def test_non_table_is_rejected(self) -> None:
        with pytest.raises(PrincipalConfigError):
            load_principals("nope")
        with pytest.raises(PrincipalConfigError):
            load_principals({"a": "nope"})

    def test_duplicate_tokens_are_rejected(self) -> None:
        digest = hash_token("same")
        with pytest.raises(PrincipalConfigError, match="same token"):
            load_principals(
                {"a": {"agent_id": "a", "token_sha256": digest}, "b": {"agent_id": "b", "token_sha256": digest}}
            )

    def test_principal_token_equal_to_legacy_token_is_rejected(self) -> None:
        with pytest.raises(PrincipalConfigError, match="auth_token"):
            load_principals({"a": {"agent_id": "a", "token_sha256": hash_token("legacy")}}, legacy_token="legacy")

    def test_legacy_token_maps_to_implicit_admin(self) -> None:
        registry = load_principals(
            {"a": {"agent_id": "a", "token_sha256": hash_token("t")}}, legacy_token="legacy"
        )
        assert registry is not None and registry.has_legacy_token
        legacy = registry.resolve_token("legacy")
        assert legacy is not None
        assert legacy.name == LEGACY_PRINCIPAL_NAME
        assert legacy.is_admin and legacy.agent_id is None and legacy.pipelines is None
        assert legacy.capabilities == CAPABILITIES

    def test_loaded_from_config_toml(self, tmp_path: Path) -> None:
        (tmp_path / ".ncp").mkdir()
        (tmp_path / ".ncp" / "config.toml").write_text(
            "[principals.router]\n"
            f'token_sha256 = "{hash_token("r")}"\n'
            'agent_id = "router"\n'
            'capabilities = ["admin"]\n',
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path)
        registry = load_principals(config.principals_config)
        assert registry is not None
        router = registry.resolve_token("r")
        assert router is not None and router.is_admin

    def test_default_config_has_no_principals(self, tmp_path: Path) -> None:
        assert load_principals(load_config(cwd=tmp_path).principals_config) is None


# ── the enforcement tables cover the whole catalog ───────────────────────────


def test_every_tool_is_classified_and_identity_args_exist_in_schema() -> None:
    names = {str(tool["name"]) for tool in MCP_TOOLS}
    classified = set(IDENTITY_ARGS) | PIPELINE_SCOPED_TOOLS | LOOKUP_SCOPED_TOOLS
    assert names <= classified, f"tools with no authorization classification: {sorted(names - classified)}"
    for tool in MCP_TOOLS:
        name = str(tool["name"])
        props = tool["inputSchema"].get("properties", {})  # type: ignore[union-attr]
        for key, _fill in IDENTITY_ARGS.get(name, ()):
            assert key in props, f"{name}.{key} is bound but not in the schema"
        if "pipeline_id" in props:
            assert name in PIPELINE_SCOPED_TOOLS, f"{name} takes pipeline_id but is not pipeline-scoped"
        # Any schema argument that plainly names an acting agent must be bound.
        for key in props:
            if key in {"agent_id", "written_by", "from", "from_agent", "sender"}:
                assert key in dict(IDENTITY_ARGS.get(name, ())), f"{name}.{key} is not bound"


# ── tool authorization ───────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


def _write(store: SQLiteStore, chunk_id: str, *, by: str, pipeline: str | None = "p1") -> None:
    store.write(
        SubconsciousChunk(
            chunk_id=chunk_id,
            layer="semantic",
            content=f"content of {chunk_id} by {by}",
            src="tool_result",
            written_by=by,
            pipeline_id=pipeline,
        )
    )


class TestIdentityBinding:
    def test_matching_identity_passes_and_omitted_is_filled(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        out = auth.check_call(_p(), "ncp_get_context", {"agent_id": "alice"})
        assert out["agent_id"] == "alice"
        filled = auth.check_call(_p(), "ncp_write_memory", {"content": "x", "layer": "semantic", "src": "synthesis"})
        assert filled["written_by"] == "alice"
        assert "owner" not in filled  # owner is checked, never filled

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("ncp_get_context", {"agent_id": "bob"}),
            ("ncp_post_turn", {"agent_id": "bob"}),
            ("ncp_write_memory", {"written_by": "bob"}),
            ("ncp_write_memory", {"owner": "bob"}),
            ("ncp_emit_whisper", {"from": "bob"}),
            ("ncp_wait_whispers", {"agent_id": "bob"}),
            ("ncp_task", {"action": "claim", "agent_id": "bob"}),
            ("ncp_overview", {"agent_id": "bob"}),
            ("ncp_fetch", {"agent_id": "bob", "query": "q"}),
            ("ncp_record_decision", {"agent_id": "bob"}),
            ("ncp_compile_decision_query", {"agent_id": "bob"}),
            ("ncp_remember", {"written_by": "bob"}),
        ],
    )
    def test_mismatched_identity_is_rejected(self, store: SQLiteStore, tool: str, args: dict) -> None:
        with pytest.raises(AuthorizationError, match="does not match"):
            ToolAuthorizer(store).check_call(_p(), tool, args)

    def test_post_turn_nested_written_by(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        with pytest.raises(AuthorizationError, match=r"memory_chunks\[1\]"):
            auth.check_call(
                _p(),
                "ncp_post_turn",
                {"agent_id": "alice", "memory_chunks": [{"written_by": "alice"}, {"written_by": "bob"}]},
            )
        ok = auth.check_call(_p(), "ncp_post_turn", {"memory_chunks": [{"content": "x"}]})
        assert ok["agent_id"] == "alice"

    def test_admin_may_act_as_anyone(self, store: SQLiteStore) -> None:
        out = ToolAuthorizer(store).check_call(_p(caps={"admin"}), "ncp_post_turn", {"agent_id": "bob"})
        assert out["agent_id"] == "bob"

    def test_input_arguments_are_not_mutated(self, store: SQLiteStore) -> None:
        args: dict[str, object] = {"content": "x"}
        ToolAuthorizer(store).check_call(_p(), "ncp_write_memory", args)
        assert args == {"content": "x"}

    def test_overwriting_another_agents_chunk_id_is_rejected(self, store: SQLiteStore) -> None:
        _write(store, "sub_bobs", by="bob")
        _write(store, "sub_alices", by="alice")
        auth = ToolAuthorizer(store)
        with pytest.raises(AuthorizationError, match="only its author"):
            auth.check_call(_p(), "ncp_write_memory", {"chunk_id": "sub_bobs"})
        with pytest.raises(AuthorizationError, match="only its author"):
            auth.check_call(_p(), "ncp_post_turn", {"memory_chunks": [{"chunk_id": "sub_bobs"}]})
        auth.check_call(_p(), "ncp_write_memory", {"chunk_id": "sub_alices"})
        auth.check_call(_p(), "ncp_write_memory", {"chunk_id": "sub_new"})

    def test_superseding_another_agents_chunk_is_rejected(self, store: SQLiteStore) -> None:
        _write(store, "sub_bobs", by="bob")
        with pytest.raises(AuthorizationError, match="supersede"):
            ToolAuthorizer(store).check_call(_p(), "ncp_write_memory", {"supersedes": "sub_bobs"})


class TestFetchBudgetKey:
    def test_session_id_is_replaced_by_principal_key(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        for tool in ("ncp_get_context", "ncp_fetch"):
            out = auth.check_call(_p(), tool, {"session_id": "fresh-every-time", "query": "q"})
            assert out["session_id"] == "principal:alice"

    def test_admin_key_is_per_acting_agent(self, store: SQLiteStore) -> None:
        out = ToolAuthorizer(store).check_call(
            _p(caps={"admin"}, name="router"), "ncp_fetch", {"agent_id": "bob", "pipeline_id": "p1", "session_id": "x"}
        )
        assert out["session_id"] == "principal:router:p1:bob"


class TestCapabilities:
    def test_user_verified_requires_verify(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        with pytest.raises(AuthorizationError, match="verify"):
            auth.check_call(_p(), "ncp_write_memory", {"src": "user_verified"})
        with pytest.raises(AuthorizationError, match="verify"):
            auth.check_call(_p(), "ncp_post_turn", {"memory_chunks": [{"src": "user_verified"}]})
        auth.check_call(_p(caps={"verify"}), "ncp_write_memory", {"src": "user_verified"})
        auth.check_call(_p(), "ncp_write_memory", {"src": "tool_result"})

    def test_verify_memo_requires_verify(self, store: SQLiteStore) -> None:
        with pytest.raises(AuthorizationError, match="verify"):
            ToolAuthorizer(store).check_call(_p(), "ncp_verify_memo", {"task": "t"})
        ToolAuthorizer(store).check_call(_p(caps={"verify"}), "ncp_verify_memo", {"task": "t"})

    def test_record_outcome_requires_capability(self, store: SQLiteStore) -> None:
        _write(store, "sub_bobs", by="bob")
        with pytest.raises(AuthorizationError, match="record_outcome"):
            ToolAuthorizer(store).check_call(_p(), "ncp_record_outcome", {"chunk_ids": ["sub_bobs"]})

    def test_record_outcome_rejects_self_credit(self, store: SQLiteStore) -> None:
        _write(store, "sub_bobs", by="bob")
        _write(store, "sub_alices", by="alice")
        auth = ToolAuthorizer(store)
        rec = _p(caps={"record_outcome"})
        auth.check_call(rec, "ncp_record_outcome", {"chunk_ids": ["sub_bobs"], "success": True})
        with pytest.raises(AuthorizationError, match="its own chunks"):
            auth.check_call(rec, "ncp_record_outcome", {"chunk_ids": ["sub_bobs", "sub_alices"]})

    def test_record_outcome_unknown_chunk_and_turn_only(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        rec = _p(caps={"record_outcome"})
        with pytest.raises(AuthorizationError, match="not a live chunk"):
            auth.check_call(rec, "ncp_record_outcome", {"chunk_ids": ["sub_missing"]})
        with pytest.raises(AuthorizationError, match="explicit chunk_ids"):
            auth.check_call(rec, "ncp_record_outcome", {"turn_id": "turn_1"})

    def test_admin_may_self_credit_and_use_turn_id(self, store: SQLiteStore) -> None:
        _write(store, "sub_alices", by="alice")
        auth = ToolAuthorizer(store)
        admin = _p(caps={"record_outcome", "admin"})
        auth.check_call(admin, "ncp_record_outcome", {"chunk_ids": ["sub_alices"]})
        auth.check_call(admin, "ncp_record_outcome", {"turn_id": "turn_1"})


class TestPipelineAcl:
    def test_named_pipeline_must_be_allowed(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        limited = _p(pipelines={"p1"})
        auth.check_call(limited, "ncp_get_context", {"pipeline_id": "p1"})
        with pytest.raises(AuthorizationError, match="may not access pipeline 'p2'"):
            auth.check_call(limited, "ncp_get_context", {"pipeline_id": "p2"})

    @pytest.mark.parametrize("tool", sorted(PIPELINE_SCOPED_TOOLS - {"ncp_task"}))
    def test_omitted_pipeline_is_rejected(self, store: SQLiteStore, tool: str) -> None:
        with pytest.raises(AuthorizationError, match="pipeline_id is required"):
            ToolAuthorizer(store).check_call(_p(pipelines={"p1"}, caps={"verify"}), tool, {"query": "q"})

    def test_unrestricted_principal_may_omit_pipeline(self, store: SQLiteStore) -> None:
        ToolAuthorizer(store).check_call(_p(), "ncp_recall", {"query": "q"})

    def test_memo_explicit_signature_is_rejected_when_limited(self, store: SQLiteStore) -> None:
        with pytest.raises(AuthorizationError, match="signature"):
            ToolAuthorizer(store).check_call(
                _p(pipelines={"p1"}), "ncp_lookup_memo", {"signature": "abc", "pipeline_id": "p1"}
            )

    def test_overview_requires_allowed_pipelines_list(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        limited = _p(pipelines={"p1"})
        with pytest.raises(AuthorizationError, match="pipelines"):
            auth.check_call(limited, "ncp_overview", {})
        with pytest.raises(AuthorizationError, match="'p2'"):
            auth.check_call(limited, "ncp_overview", {"pipelines": ["p1", "p2"]})
        auth.check_call(limited, "ncp_overview", {"pipelines": ["p1"]})

    def test_record_outcome_chunks_must_be_in_allowed_pipelines(self, store: SQLiteStore) -> None:
        _write(store, "sub_p2", by="bob", pipeline="p2")
        with pytest.raises(AuthorizationError, match="outside"):
            ToolAuthorizer(store).check_call(
                _p(pipelines={"p1"}, caps={"record_outcome", "admin"}),
                "ncp_record_outcome",
                {"chunk_ids": ["sub_p2"]},
            )

    def test_get_decision_result_is_hidden_outside_pipelines(self, store: SQLiteStore) -> None:
        auth = ToolAuthorizer(store)
        result = {"found": True, "decision": {"decision_id": "d1", "pipeline_id": "p2"}}
        assert auth.filter_result(_p(pipelines={"p1"}), "ncp_get_decision", result) == {
            "found": False,
            "decision_id": "d1",
        }
        assert auth.filter_result(_p(), "ncp_get_decision", result) is result


class TestReadAuthorization:
    def test_unscoped_read_requires_admin(self) -> None:
        with pytest.raises(AuthorizationError, match="admin"):
            authorize_read(_p(), pipeline_id=None, endpoint="/api/chunks")
        authorize_read(_p(caps={"admin"}), pipeline_id=None, endpoint="/api/chunks")
        authorize_read(_p(), pipeline_id="p9", endpoint="/api/chunks")

    def test_pipeline_acl_on_reads(self) -> None:
        limited = _p(pipelines={"p1"}, caps={"admin"})
        authorize_read(limited, pipeline_id="p1", endpoint="/api/chunks")
        with pytest.raises(AuthorizationError):
            authorize_read(limited, pipeline_id="p2", endpoint="/api/chunks")
        with pytest.raises(AuthorizationError):
            authorize_read(limited, pipeline_id=None, endpoint="/api/chunks")

    def test_events_binding(self) -> None:
        assert authorize_events(_p(), agent_id=None, pipeline_id=None) == "alice"
        assert authorize_events(_p(), agent_id="alice", pipeline_id=None) == "alice"
        with pytest.raises(AuthorizationError, match="does not match"):
            authorize_events(_p(), agent_id="bob", pipeline_id=None)
        with pytest.raises(AuthorizationError, match="pipeline_id is required"):
            authorize_events(_p(pipelines={"p1"}), agent_id=None, pipeline_id=None)
        assert authorize_events(_p(caps={"admin"}), agent_id="bob", pipeline_id=None) == "bob"
        assert authorize_events(_p(caps={"admin"}), agent_id=None, pipeline_id=None) is None
