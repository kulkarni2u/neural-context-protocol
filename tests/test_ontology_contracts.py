from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ncp.config import load_config
from ncp.ontology.models import Entity, EntityMatch, GraphQuery, Relation, Scope
from ncp.ontology.policy import OntologyConfig
from ncp.ontology.protocol import DisabledOntologyProvider
from ncp.ontology.registry import SchemaPack, SchemaRegistry

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


@pytest.fixture
def endpoints():
    scope = Scope(tenant_id="payroll", namespace="production")
    source = Entity(
        scope=scope, entity_type="Service", canonical_key=" Payroll / PROD ",
        display_name="Payroll", aliases=("PAYROLL", " payroll ", "Wages"),
        attributes={"name": "Payroll", "environment": "prod"}, schema_pack="infrastructure",
    )
    target = Entity(
        scope=scope, entity_type="KafkaTopic", canonical_key="prod/payroll-events",
        display_name="Payroll events", attributes={"name": "payroll-events", "cluster": "prod"},
        schema_pack="infrastructure",
    )
    return source, target


def relation(source, target, **overrides):
    return Relation.model_validate({
        "relation_id": uuid4(),
        "scope": source.scope,
        "src_entity_id": source.entity_id,
        "dst_entity_id": target.entity_id,
        "relation_type": "CONSUMES",
        "confidence": 0.95,
        "valid_from": NOW,
        "observed_at": NOW,
        "created_by": "catalog",
        "schema_pack": "infrastructure",
        **overrides,
    })


def test_schema_pack_validation(endpoints):
    source, target = endpoints
    registry = SchemaRegistry.builtin()
    registry.validate_relation(relation(source, target), source=source, target=target)
    with pytest.raises(ValueError, match="triple"):
        registry.validate_relation(
            relation(source, target, relation_type="OWNS"), source=source, target=target,
        )
    with pytest.raises(ValueError, match="missing required attribute"):
        registry.validate_entity(source.model_copy(update={"attributes": {"name": "Payroll"}}))
    with pytest.raises(ValueError, match="unknown schema pack version"):
        registry.validate_entity(source.model_copy(update={"schema_version": 2}))
    with pytest.raises(ValueError, match="endpoint IDs"):
        registry.validate_relation(
            relation(source, target, dst_entity_id=uuid4()), source=source, target=target,
        )


@pytest.mark.parametrize("scope", [
    Scope(tenant_id="other", namespace="production"),
    Scope(tenant_id="payroll", namespace="staging"),
])
def test_cross_scope_relationships_rejected(endpoints, scope):
    source, target = endpoints
    target = target.model_copy(update={"scope": scope})
    with pytest.raises(ValueError, match="share its scope"):
        SchemaRegistry.builtin().validate_relation(
            relation(source, target), source=source, target=target,
        )


def test_identity_and_alias_ambiguity(endpoints):
    source, _ = endpoints
    same = Entity.model_validate({**source.model_dump(), "canonical_key": "PAYROLL / prod"})
    other = Entity.model_validate({**source.model_dump(), "canonical_key": "payroll / stage"})
    assert same.entity_id == source.entity_id
    assert other.entity_id != source.entity_id
    assert source.aliases == ("payroll", "wages")
    assert EntityMatch(entity_ids=(source.entity_id,), matched_key="payroll").resolved_id == source.entity_id
    assert EntityMatch(
        entity_ids=(source.entity_id, other.entity_id), matched_key="payroll",
    ).resolved_id is None


@pytest.mark.parametrize("overrides", [
    {"status": "verified"}, {"valid_until": NOW}, {"confidence": 1.01},
    {"observed_at": NOW.replace(tzinfo=None)}, {"unexpected": True},
])
def test_invalid_relation_shape(endpoints, overrides):
    with pytest.raises(ValidationError):
        relation(*endpoints, **overrides)


def test_schema_versions_are_immutable():
    data = {
        "id": "example", "version": 1,
        "entity_types": {"Service": {}},
        "relation_types": {"LINKS": {"from": ["Service"], "to": ["Unknown"]}},
    }
    with pytest.raises(ValidationError, match="unknown entity type"):
        SchemaPack.model_validate(data)
    data["relation_types"]["LINKS"]["to"] = ["Service"]
    pack = SchemaPack.model_validate(data)
    registry = SchemaRegistry()
    registry.register(pack)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(pack)


def test_mode_precedence():
    assert OntologyConfig().resolve_mode(override="always") == "off"
    assert OntologyConfig(enabled=True, provider="disabled").resolve_mode() == "off"
    config = OntologyConfig(enabled=True)
    assert config.resolve_mode() == "auto"
    assert config.resolve_mode(opt_out=True, override="always") == "off"
    assert config.resolve_mode(override="always", override_authorized=True) == "always"
    assert OntologyConfig(enabled=True, retrieval_mode="off").resolve_mode() == "off"
    with pytest.raises(PermissionError):
        config.resolve_mode(override="always")
    with pytest.raises(ValidationError):
        GraphQuery(max_hops=6)


def test_config_loading_is_opt_in(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path, cwd=tmp_path, env={}).ontology.resolve_mode() == "off"
    path.write_text('[ontology]\nenabled = true\nretrieval_mode = "always"\n')
    assert load_config(path, cwd=tmp_path, env={}).ontology.resolve_mode() == "always"
    path.write_text('[ontology]\nenabled = "false"\n')
    with pytest.raises(ValidationError):
        load_config(path, cwd=tmp_path, env={})


def test_disabled_provider_capabilities():
    assert DisabledOntologyProvider().capabilities() == {"enabled": False, "version": "1"}


def test_disabled_config_preserves_context_and_tool_catalog(tmp_path):
    from ncp.api import agent, get_context
    from ncp.mcp.server import tools_for_config
    from ncp.stores.sqlite import SQLiteStore

    path = tmp_path / "config.toml"
    default = load_config(path, cwd=tmp_path, env={})
    path.write_text('[ontology]\nenabled = false\nretrieval_mode = "always"\n')
    disabled = load_config(path, cwd=tmp_path, env={})
    conscious = agent(id="builder", role="build", task="test_context", slot="test", intent="test")

    assert tools_for_config(default) == tools_for_config(disabled)
    assert get_context(
        agent=conscious, store=SQLiteStore(tmp_path / "default.db"), config=default,
    ) == get_context(
        agent=conscious, store=SQLiteStore(tmp_path / "disabled.db"), config=disabled,
    )
