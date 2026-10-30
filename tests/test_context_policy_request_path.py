"""Context policies selected per request on ncp_get_context and ncp.api."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ncp import api
from ncp.config import NCPConfig, load_config
from ncp.context_policy import UnknownContextPolicyError, resolve_context_policy
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import SubconsciousChunk

PIPELINE = "pipe_policy"


def _call(arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ncp_get_context", "arguments": arguments},
    }


def _get_context(handlers, **overrides: object) -> dict:
    response = json.loads(_handle_request(_call(_args(**overrides)), handlers))
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _get_context_error(handlers, **overrides: object) -> str:
    response = json.loads(_handle_request(_call(_args(**overrides)), handlers))
    assert "error" in response, response
    return response["error"]["message"]


def _args(**overrides: object) -> dict:
    base = {
        "agent_id": "builder",
        "role": "build",
        "owns": [],
        "must_not": [],
        "task": "policy",
        "slot": "context",
        "intent": "test",
        "pipeline_id": PIPELINE,
    }
    base.update(overrides)
    return base


def _seed(store: SQLiteStore, count: int = 6) -> None:
    for index in range(count):
        store.write(SubconsciousChunk(
            chunk_id=f"policy_chunk_{index}",
            content=" ".join(["policy context"] + [f"tokenish_{index}_{j}" for j in range(80)]),
            layer="semantic",
            src="tool_result",
            pipeline_id=PIPELINE,
        ))


def _config(tmp_path: Path) -> NCPConfig:
    config = load_config(cwd=tmp_path)
    config.values["budget"]["adaptive_budget_enabled"] = False
    config.values["budget"]["context_token_budget"] = 3000
    config.values["budget"]["max_tokens_per_call"] = 4000
    config.values["budget"]["adaptive_budget_floor_tokens"] = 100
    config.values["distillation"]["enabled"] = False
    config.values["context_policies"] = {
        "tight-v1": {"version": 1, "budget": {"context_token_budget": 150}},
        "lean-v2": {
            "version": 2,
            "budget": {"adaptive_budget_enabled": True, "adaptive_budget_ceiling_tokens": 200},
            "distillation": {"enabled": True},
        },
    }
    return config


class TestMcpGetContext:
    def test_no_policy_has_no_block_and_matches_legacy_context(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "legacy.db")
        _seed(store)
        config = _config(tmp_path)
        legacy = copy.deepcopy(config)
        del legacy.values["context_policies"]

        with_registry = _get_context(make_handlers(store, config=config))
        without_registry = _get_context(make_handlers(store, config=legacy))

        assert "context_policy" not in with_registry
        assert with_registry["context"] == without_registry["context"]

    def test_selected_policy_reports_identity_and_applies_budget(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "tight.db")
        _seed(store)
        config = _config(tmp_path)
        handlers = make_handlers(store, config=config)

        baseline = _get_context(handlers)
        result = _get_context(handlers, context_policy="tight-v1")

        identity = resolve_context_policy(config, "tight-v1").identity
        assert result["context_policy"] == {
            "name": "tight-v1",
            "version": 1,
            "sha256": identity.sha256,
            "max_tokens_cap": 4000,
        }
        assert estimate_tokens(baseline["context"]) > 150
        assert estimate_tokens(result["context"]) <= 150

    def test_policy_does_not_leak_into_later_requests(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "leak.db")
        _seed(store)
        config = _config(tmp_path)
        before = copy.deepcopy(config.values)
        handlers = make_handlers(store, config=config)

        first = _get_context(handlers)
        _get_context(handlers, context_policy="tight-v1")
        after = _get_context(handlers)

        assert config.values == before
        assert after["context"] == first["context"]
        assert "context_policy" not in after

    def test_policy_caps_explicit_max_tokens(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "cap.db")
        _seed(store)
        config = _config(tmp_path)
        config.values["budget"]["context_token_budget"] = 150
        config.values["budget"]["max_tokens_per_call"] = 200
        config.values["budget"]["adaptive_budget_ceiling_tokens"] = 200
        handlers = make_handlers(store, config=config)

        uncapped = _get_context(handlers, max_tokens=5000)
        capped = _get_context(handlers, max_tokens=5000, context_policy="tight-v1")

        assert estimate_tokens(uncapped["context"]) > 200
        assert estimate_tokens(capped["context"]) <= 200
        assert capped["context_policy"]["max_tokens_cap"] == 200
        assert capped["context_policy"]["max_tokens_capped_from"] == 5000

    def test_explicit_max_tokens_within_cap_is_not_reported_as_capped(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "within.db")
        config = _config(tmp_path)
        handlers = make_handlers(store, config=config)

        result = _get_context(handlers, max_tokens=100, context_policy="tight-v1")

        assert "max_tokens_capped_from" not in result["context_policy"]

    def test_policy_drives_adaptive_budget_and_feature_telemetry(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive.db")
        _seed(store)
        config = _config(tmp_path)
        handlers = make_handlers(store, config=config)

        baseline = _get_context(handlers)
        result = _get_context(handlers, context_policy="lean-v2")

        assert "budget_tokens" not in baseline
        assert baseline["telemetry"]["active_features"]["adaptive_budget_enabled"] is False
        assert baseline["telemetry"]["active_features"]["distillation_enabled"] is False
        assert result["budget_tokens"]["adjusted"] <= 200
        assert estimate_tokens(result["context"]) <= result["budget_tokens"]["adjusted"]
        assert result["telemetry"]["active_features"]["adaptive_budget_enabled"] is True
        assert result["telemetry"]["active_features"]["distillation_enabled"] is True

    def test_stream_response_carries_policy_block(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "stream.db")
        config = _config(tmp_path)
        handlers = make_handlers(store, config=config)

        streamed = handlers["ncp_get_context"](_args(stream=True, context_policy="tight-v1"))

        assert streamed.handler_result["context_policy"]["name"] == "tight-v1"

    def test_unknown_policy_is_rejected(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "unknown.db")
        handlers = make_handlers(store, config=_config(tmp_path))

        message = _get_context_error(handlers, context_policy="missing-v9")

        assert "Unknown context policy 'missing-v9'" in message

    def test_invalid_policy_is_rejected_without_echoing_values(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "invalid.db")
        config = _config(tmp_path)
        config.values["context_policies"]["bad-v1"] = {"version": 1, "retrieval": {"api_key": "sk-secret"}}
        handlers = make_handlers(store, config=config)

        message = _get_context_error(handlers, context_policy="bad-v1")

        assert "bad-v1" in message
        assert "sk-secret" not in message

    def test_policy_without_config_is_rejected(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "noconfig.db")
        handlers = make_handlers(store)

        message = _get_context_error(handlers, context_policy="tight-v1")

        assert "requires a loaded NCP config" in message


class TestPythonApi:
    def test_get_context_applies_selected_policy(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "api.db")
        _seed(store)
        config = _config(tmp_path)
        agent = api.agent(id="builder", role="build", task="policy", slot="context", pipeline_id=PIPELINE)

        baseline = api.get_context(agent=agent, store=store, config=config)
        tight = api.get_context(agent=agent, store=store, config=config, context_policy="tight-v1")

        assert estimate_tokens(baseline) > 150
        assert estimate_tokens(tight) <= 150

    def test_get_context_caps_explicit_max_tokens_only_with_policy(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "api_cap.db")
        _seed(store)
        config = _config(tmp_path)
        config.values["budget"]["context_token_budget"] = 150
        config.values["budget"]["max_tokens_per_call"] = 200
        config.values["budget"]["adaptive_budget_ceiling_tokens"] = 200
        agent = api.agent(id="builder", role="build", task="policy", slot="context", pipeline_id=PIPELINE)

        uncapped = api.get_context(agent=agent, store=store, config=config, max_tokens=5000)
        capped = api.get_context(
            agent=agent, store=store, config=config, max_tokens=5000, context_policy="tight-v1"
        )

        assert estimate_tokens(uncapped) > 200
        assert estimate_tokens(capped) <= 200

    def test_get_context_rejects_unknown_policy(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "api_unknown.db")
        agent = api.agent(id="builder", role="build", task="policy", slot="context", pipeline_id=PIPELINE)

        with pytest.raises(UnknownContextPolicyError):
            api.get_context(agent=agent, store=store, config=_config(tmp_path), context_policy="missing-v9")
