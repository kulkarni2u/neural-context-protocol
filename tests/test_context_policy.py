from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest

from ncp.config import DEFAULT_CONFIG, NCPConfig, load_config
from ncp.context_policy import (
    ContextPolicy,
    ContextPolicyError,
    ContextPolicyRegistry,
    UnknownContextPolicyError,
    resolve_context_policy,
)


def make_config(policies=None, **sections) -> NCPConfig:
    values = copy.deepcopy(DEFAULT_CONFIG)
    for section, updates in sections.items():
        values[section].update(updates)
    if policies is not None:
        values["context_policies"] = policies
    return NCPConfig(values=values, project_root=Path("/tmp/ncp-project"))


BALANCED = {
    "name": "balanced-v1",
    "version": 1,
    "retrieval": {"diversity_lambda": 0.7, "edge_max_hops": 2,
                  "edge_expansion_types": ["caused_by", "supports"]},
    "budget": {"context_token_budget": 900, "adaptive_budget_floor_tokens": 300,
               "adaptive_budget_ceiling_tokens": 1800},
    "distillation": {"enabled": True, "min_chunk_tokens": 100},
}


def cfg_with(policy: dict, name: str = "p", **sections) -> NCPConfig:
    return make_config({name: {"name": name, "version": 1, **policy}}, **sections)


def test_valid_policy_resolves_overlay_and_identity():
    config = make_config({"balanced-v1": BALANCED})
    result = resolve_context_policy(config, "balanced-v1")
    assert result.identity.name == "balanced-v1"
    assert result.identity.version == 1
    assert len(result.identity.sha256) == 64
    assert result.config.diversity_lambda == 0.7
    assert result.config.edge_max_hops == 2
    assert result.config.edge_expansion_types == ["caused_by", "supports"]
    assert result.config.context_token_budget == 900
    assert result.config.adaptive_budget_ceiling_tokens == 1800
    assert result.config.distillation_min_chunk_tokens == 100
    assert result.config.project_root == config.project_root
    assert result.config is not config


def test_key_order_does_not_change_hash():
    a = {"name": "p", "version": 2, "retrieval": {"diversity_lambda": 0.5, "edge_max_hops": 1}}
    b = {"retrieval": {"edge_max_hops": 1, "diversity_lambda": 0.5}, "version": 2, "name": "p"}
    ia = resolve_context_policy(make_config({"p": a}), "p").identity
    ib = resolve_context_policy(make_config({"p": b}), "p").identity
    assert ia == ib
    c = {**a, "version": 3}
    assert resolve_context_policy(make_config({"p": c}), "p").identity.sha256 != ia.sha256


def test_none_selection_returns_same_config_even_with_invalid_unused_policy():
    config = make_config({"bad": {"name": "bad", "version": 0, "bogus": 1}})
    result = resolve_context_policy(config, None)
    assert result.config is config
    assert result.identity is None


def test_selecting_valid_policy_ignores_other_invalid_entries():
    config = make_config({"ok": {"name": "ok", "version": 1}, "bad": {"version": 0}})
    assert resolve_context_policy(config, "ok").identity.name == "ok"


def test_registry_load_validates_everything():
    config = make_config({"ok": {"name": "ok", "version": 1}, "bad": {"version": 0}})
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(config)


def test_registry_names_and_get_returns_copy():
    config = make_config({"balanced-v1": BALANCED})
    reg = ContextPolicyRegistry.from_config(config)
    assert reg.names() == ["balanced-v1"]
    got = reg.get("balanced-v1")
    assert isinstance(got, ContextPolicy)
    got.retrieval.edge_expansion_types.append("related")
    assert reg.get("balanced-v1").retrieval.edge_expansion_types == ["caused_by", "supports"]


def test_unknown_name_fails_clearly():
    with pytest.raises(UnknownContextPolicyError, match="nope"):
        resolve_context_policy(make_config({}), "nope")
    with pytest.raises(UnknownContextPolicyError):
        resolve_context_policy(make_config(), "nope")


def test_embedded_name_mismatch_rejected():
    config = make_config({"a": {"name": "b", "version": 1}})
    with pytest.raises(ContextPolicyError, match="name"):
        resolve_context_policy(config, "a")


def test_embedded_name_optional():
    config = make_config({"a": {"version": 1}})
    assert resolve_context_policy(config, "a").identity.name == "a"


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.0, None])
def test_bad_version_rejected(version):
    config = make_config({"a": {"name": "a", "version": version}})
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(config, "a")


def test_missing_version_rejected():
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(make_config({"a": {"name": "a"}}), "a")


@pytest.mark.parametrize(
    "policy",
    [
        {"bogus": 1},
        {"retrieval": {"rerank_enabled": True}},
        {"retrieval": {"trust_propagation_factor": 0.1}},
        {"budget": {"max_tokens_per_call": 100}},
        {"budget": {"pipeline_budget_usd": 1.0}},
        {"distillation": {"bogus": 1}},
        {"store": {"type": "sqlite"}},
        {"server": {"auth_token": "x"}},
        {"memoization": {"enabled": True}},
        {"embedding": {"provider": "x"}},
    ],
)
def test_unsupported_fields_rejected(policy):
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(cfg_with(policy), "p")


@pytest.mark.parametrize(
    "policy",
    [
        {"retrieval": {"edge_expansion": "yes"}},
        {"retrieval": {"edge_expansion": 1}},
        {"retrieval": {"reduce_fanin_enabled": "true"}},
        {"budget": {"adaptive_budget_enabled": 1}},
        {"distillation": {"enabled": "false"}},
        {"budget": {"context_token_budget": "800"}},
        {"budget": {"context_token_budget": True}},
        {"budget": {"context_token_budget": 800.5}},
        {"retrieval": {"diversity_lambda": "0.5"}},
        {"retrieval": {"diversity_lambda": True}},
    ],
)
def test_coercive_values_rejected(policy):
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(cfg_with(policy), "p")


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize(
    "field",
    ["diversity_lambda", "edge_expansion_decay", "reduce_fanin_similarity_threshold",
     "reduce_fanin_contradict_floor"],
)
def test_non_finite_rejected(field, bad):
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(cfg_with({"retrieval": {field: bad}}), "p")


@pytest.mark.parametrize(
    "retrieval",
    [
        {"edge_expansion_types": ["nonsense"]},
        {"edge_expansion_types": "caused_by"},
        {"edge_max_hops": -1},
        {"reduce_fanin_min_cluster": 0},
        {"reduce_fanin_overfetch": 0},
        {"diversity_lambda": 1.5},
        {"diversity_lambda": -0.1},
        {"edge_expansion_decay": 2},
        {"reduce_fanin_similarity_threshold": -0.5},
        {"reduce_fanin_contradict_floor": 1.01},
    ],
)
def test_invalid_retrieval_values_rejected(retrieval):
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(cfg_with({"retrieval": retrieval}), "p")


@pytest.mark.parametrize(
    "policy",
    [
        {"budget": {"context_token_budget": 0}},
        {"budget": {"adaptive_budget_floor_tokens": 0}},
        {"budget": {"adaptive_budget_ceiling_tokens": -5}},
        {"distillation": {"min_chunk_tokens": 0}},
    ],
)
def test_invalid_sizes_rejected(policy):
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(cfg_with(policy), "p")


def test_inverted_explicit_budget_range_rejected():
    policy = {"budget": {"adaptive_budget_floor_tokens": 900, "adaptive_budget_ceiling_tokens": 500}}
    with pytest.raises(ContextPolicyError, match="floor"):
        resolve_context_policy(cfg_with(policy), "p")


def test_inverted_inherited_range_rejected():
    # Policy only raises the floor; deployment ceiling (2000) is inherited.
    policy = {"budget": {"adaptive_budget_floor_tokens": 2500}}
    with pytest.raises(ContextPolicyError, match="floor"):
        resolve_context_policy(cfg_with(policy, budget={"max_tokens_per_call": 5000}), "p")


def test_context_budget_over_deployment_max_rejected():
    with pytest.raises(ContextPolicyError, match="max_tokens_per_call"):
        resolve_context_policy(cfg_with({"budget": {"context_token_budget": 4001}}), "p")


def test_ceiling_over_deployment_max_rejected():
    with pytest.raises(ContextPolicyError, match="max_tokens_per_call"):
        resolve_context_policy(
            cfg_with({"budget": {"adaptive_budget_ceiling_tokens": 4001}}), "p"
        )


def test_budget_equal_to_max_allowed():
    result = resolve_context_policy(
        cfg_with({"budget": {"context_token_budget": 4000, "adaptive_budget_ceiling_tokens": 4000}}),
        "p",
    )
    assert result.config.context_token_budget == 4000


@pytest.mark.parametrize("bad_max", [0, -1, True, "4000", None, 12.5])
def test_invalid_deployment_max_rejected_on_selection(bad_max):
    config = cfg_with({}, budget={"max_tokens_per_call": bad_max})
    with pytest.raises(ContextPolicyError, match="max_tokens_per_call"):
        resolve_context_policy(config, "p")


def test_invalid_deployment_max_ignored_without_selection():
    config = make_config({}, budget={"max_tokens_per_call": -1})
    assert resolve_context_policy(config, None).config is config


def test_resolution_does_not_mutate_inputs():
    config = make_config({"balanced-v1": copy.deepcopy(BALANCED)})
    before_values = copy.deepcopy(config.values)
    first = resolve_context_policy(config, "balanced-v1")
    assert config.values == before_values
    first.config.values["retrieval"]["edge_expansion_types"].append("related")
    first.config.values["budget"]["context_token_budget"] = 1
    first.config.values["context_policies"]["balanced-v1"]["version"] = 99
    assert config.values == before_values
    second = resolve_context_policy(config, "balanced-v1")
    assert second.config.edge_expansion_types == ["caused_by", "supports"]
    assert second.config.context_token_budget == 900
    assert second.config is not first.config
    assert second.config.values is not first.config.values


def test_registry_entries_not_aliased_to_config():
    raw = copy.deepcopy(BALANCED)
    config = make_config({"balanced-v1": raw})
    reg = ContextPolicyRegistry.from_config(config)
    raw["retrieval"]["edge_expansion_types"].append("related")
    assert reg.get("balanced-v1").retrieval.edge_expansion_types == ["caused_by", "supports"]


def test_unrelated_settings_unchanged():
    config = make_config(
        {"balanced-v1": BALANCED},
        server={"auth_token": "secret-token"},
        store={"type": "pgvector"},
    )
    result = resolve_context_policy(config, "balanced-v1")
    allowed = {
        ("retrieval", k) for k in BALANCED["retrieval"]
    } | {("budget", k) for k in BALANCED["budget"]} | {
        ("distillation", k) for k in BALANCED["distillation"]
    }
    for section, body in config.values.items():
        if not isinstance(body, dict):
            assert result.config.values[section] == body
            continue
        for key, value in body.items():
            if (section, key) not in allowed:
                assert result.config.values[section][key] == value, (section, key)
    assert result.config.server_auth_token == "secret-token"
    assert result.config.values["store"] == config.values["store"]


def test_hash_excludes_unrelated_deployment_config():
    a = resolve_context_policy(make_config({"p": {"version": 1}}), "p").identity
    b = resolve_context_policy(
        make_config({"p": {"version": 1}}, server={"auth_token": "x"}, pipeline={"gc_threshold": 1}),
        "p",
    ).identity
    assert a == b


@pytest.mark.parametrize(
    "store",
    [
        {"type": "sqlite", "path": ".ncp/store.db"},
        {"type": "pgvector", "path": ".ncp/store.db"},
    ],
)
def test_resolution_is_store_independent(store):
    config = make_config({"balanced-v1": BALANCED}, store=store)
    result = resolve_context_policy(config, "balanced-v1")
    assert result.config.values["store"] == store
    assert result.config.context_token_budget == 900


def test_async_pgvector_settings_preserved():
    config = make_config(
        {"balanced-v1": BALANCED},
        store={"type": "pgvector"},
        pgvector={"dsn": "postgresql://u:p@h/db", "schema": "s", "table_prefix": "x_"},
    )
    result = resolve_context_policy(config, "balanced-v1")
    assert result.config.pgvector_dsn == "postgresql://u:p@h/db"
    assert result.config.pgvector_schema == "s"
    assert result.config.pgvector_table_prefix == "x_"
    assert result.identity == resolve_context_policy(
        make_config({"balanced-v1": BALANCED}), "balanced-v1"
    ).identity


def test_loaded_toml_policy(tmp_path):
    ncp_dir = tmp_path / ".ncp"
    ncp_dir.mkdir()
    (ncp_dir / "config.toml").write_text(
        "[context_policies.balanced-v1]\n"
        "version = 1\n"
        "[context_policies.balanced-v1.retrieval]\n"
        "diversity_lambda = 0.8\n"
        'edge_expansion_types = ["caused_by"]\n'
        "[context_policies.balanced-v1.budget]\n"
        "context_token_budget = 700\n"
    )
    config = load_config(cwd=tmp_path, env={})
    result = resolve_context_policy(config, "balanced-v1")
    assert result.config.diversity_lambda == 0.8
    assert result.config.context_token_budget == 700
    assert result.config.project_root == config.project_root


def test_malformed_registry_shapes_rejected():
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(make_config("not-a-table"))
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(make_config({"a": "not-a-table"}))


def test_empty_name_rejected():
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(make_config({"": {"version": 1}}))


def test_documented_examples_validate():
    import re

    text = (Path(__file__).resolve().parents[1] / "docs" / "CONTEXT_POLICY.md").read_text()
    blocks = re.findall(r"```toml\n(.*?)```", text, re.S)
    assert blocks
    import tomllib

    for block in blocks:
        parsed = tomllib.loads(block)
        config = make_config(parsed["context_policies"])
        for name in parsed["context_policies"]:
            assert resolve_context_policy(config, name).identity.name == name


# --- review fixes -----------------------------------------------------------

SENTINEL = "SENTINEL-s3cr3t-token"


def test_model_rejects_explicit_inverted_budget_range():
    with pytest.raises(ValueError):
        ContextPolicy.model_validate(
            {"name": "p", "version": 1,
             "budget": {"adaptive_budget_floor_tokens": 1000, "adaptive_budget_ceiling_tokens": 500}}
        )


def test_registry_rejects_explicit_inverted_range_without_selection():
    policy = {"version": 1, "budget": {"adaptive_budget_floor_tokens": 1000,
                                       "adaptive_budget_ceiling_tokens": 500}}
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(make_config({"p": policy}))
    with pytest.raises(ContextPolicyError):
        resolve_context_policy(make_config({"p": policy}), "p")


def test_equal_and_partial_ranges_still_allowed_by_model():
    ContextPolicy.model_validate(
        {"name": "p", "version": 1,
         "budget": {"adaptive_budget_floor_tokens": 500, "adaptive_budget_ceiling_tokens": 500}}
    )
    ContextPolicy.model_validate(
        {"name": "p", "version": 1, "budget": {"adaptive_budget_floor_tokens": 5000}}
    )


@pytest.mark.parametrize(
    "policy",
    [
        {"server": {"auth_token": SENTINEL}},
        {"retrieval": {"diversity_lambda": SENTINEL}},
        {"budget": {"context_token_budget": SENTINEL}},
        {"retrieval": {"edge_expansion_types": [SENTINEL]}},
        {"version": SENTINEL},
    ],
)
def test_validation_errors_do_not_echo_input_values(policy):
    raw = {"name": "p", "version": 1, **policy}
    config = make_config({"p": raw})
    for call in (
        lambda: resolve_context_policy(config, "p"),
        lambda: ContextPolicyRegistry.from_config(config),
    ):
        with pytest.raises(ContextPolicyError) as info:
            call()
        text = str(info.value) + repr(info.value)
        assert SENTINEL not in text
        assert info.value.__cause__ is None or SENTINEL not in str(info.value.__cause__)


def test_validation_error_keeps_field_path_and_reason():
    config = make_config({"p": {"version": 1, "server": {"auth_token": SENTINEL}}})
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, "p")
    assert "server" in str(info.value)
    assert "not permitted" in str(info.value)
    config = make_config({"p": {"version": 1, "budget": {"context_token_budget": SENTINEL}}})
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, "p")
    assert "budget.context_token_budget" in str(info.value)
    assert "integer" in str(info.value)


def test_registry_errors_do_not_echo_rejected_names_or_values():
    for registry in (
        {"a": {"name": SENTINEL, "version": 1}},
        {"a": SENTINEL},
        {"a": [SENTINEL]},
    ):
        with pytest.raises(ContextPolicyError) as info:
            ContextPolicyRegistry.from_config(make_config(registry))
        assert SENTINEL not in str(info.value)
    with pytest.raises(ContextPolicyError) as info:
        ContextPolicyRegistry.from_config(make_config(SENTINEL))
    assert SENTINEL not in str(info.value)
    with pytest.raises(ContextPolicyError):
        ContextPolicyRegistry.from_config(make_config({5: {"version": 1}}))


def test_deployment_budget_errors_do_not_echo_values():
    config = cfg_with({}, budget={"max_tokens_per_call": SENTINEL})
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, "p")
    assert SENTINEL not in str(info.value)
    config = cfg_with({}, budget={"context_token_budget": SENTINEL})
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, "p")
    assert SENTINEL not in str(info.value)


# --- final small fix: malformed names and selected config sections ---------


@pytest.mark.parametrize("bad_name", [[], {}, 5, ("a",)])
def test_non_string_selection_fails_as_policy_error(bad_name):
    config = make_config({"balanced-v1": BALANCED})
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, bad_name)
    assert not isinstance(info.value, TypeError)
    with pytest.raises(ContextPolicyError) as info:
        ContextPolicyRegistry.from_config(config).get(bad_name)
    assert not isinstance(info.value, TypeError)


def test_empty_string_selection_fails_clearly():
    config = make_config({"balanced-v1": BALANCED})
    with pytest.raises(ContextPolicyError, match="non-empty|Unknown"):
        resolve_context_policy(config, "")
    with pytest.raises(ContextPolicyError, match="non-empty|Unknown"):
        ContextPolicyRegistry.from_config(config).get("")


def test_none_selection_is_still_noop_for_malformed_names_registry():
    config = make_config({"balanced-v1": BALANCED})
    assert resolve_context_policy(config, None).config is config


def _config_with_section(section, value, policies=None):
    values = copy.deepcopy(DEFAULT_CONFIG)
    values[section] = value
    values["context_policies"] = policies if policies is not None else {"p": {"name": "p", "version": 1, "retrieval": {"diversity_lambda": 0.5}}}
    return NCPConfig(values=values, project_root=Path("/tmp/ncp-project"))


@pytest.mark.parametrize("bad", ["scalar", None, 5, ["x"]])
@pytest.mark.parametrize(
    "section,policy",
    [
        ("retrieval", {"retrieval": {"diversity_lambda": 0.5}}),
        ("distillation", {"distillation": {"enabled": True}}),
        ("budget", {"budget": {"context_token_budget": 100}}),
    ],
)
def test_malformed_overlaid_section_rejected(section, policy, bad):
    config = _config_with_section(
        section, bad, {"p": {"name": "p", "version": 1, **policy}}
    )
    with pytest.raises(ContextPolicyError, match=f"Deployment {section} must be a table|budget"):
        resolve_context_policy(config, "p")


def test_malformed_budget_rejected_even_when_policy_does_not_overlay_budget():
    config = _config_with_section("budget", "scalar")
    with pytest.raises(ContextPolicyError, match="budget"):
        resolve_context_policy(config, "p")


def test_malformed_unrelated_section_not_validated_by_selection():
    config = _config_with_section("distillation", None)
    result = resolve_context_policy(config, "p")
    assert result.identity.name == "p"
    assert result.config.values["distillation"] is None


def test_malformed_section_without_selection_keeps_config_identity():
    config = _config_with_section("budget", "scalar")
    result = resolve_context_policy(config, None)
    assert result.config is config
    assert result.identity is None


def test_malformed_section_errors_do_not_echo_values():
    config = _config_with_section(
        "retrieval", SENTINEL, {"p": {"name": "p", "version": 1, "retrieval": {"diversity_lambda": 0.5}}}
    )
    with pytest.raises(ContextPolicyError) as info:
        resolve_context_policy(config, "p")
    assert SENTINEL not in str(info.value) and SENTINEL not in repr(info.value)
