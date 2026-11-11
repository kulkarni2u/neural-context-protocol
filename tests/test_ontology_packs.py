"""Custom schema packs: loading, extension, validation, config wiring and the pack CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.ontology.facade import DEFAULT_TENANT, OntologyFacade
from ncp.ontology.models import AuthContext, Scope
from ncp.ontology.registry import PackError, SchemaRegistry
from ncp.stores.sqlite import SQLiteStore

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "13_ontology" / "packs" / "payments.v1.json"

EXTENSION = {
    "id": "infrastructure",
    "version": 9,
    "extends": {"id": "infrastructure", "version": 2},
    "entity_types": {"Queue": {"required": ["name"], "tag": "queue"}},
    "relation_types": {
        "PRODUCES": {"from": ["Queue"], "to": ["KafkaTopic"]},
        "ROUTES_TO": {"from": ["Queue"], "to": ["Queue", "Service"]},
    },
}


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_new_id_pack_loads_from_example() -> None:
    registry = SchemaRegistry.builtin([EXAMPLE])
    assert registry.has_version("payments", 1)
    assert registry.source("payments", 1) == str(EXAMPLE)
    assert registry.source("infrastructure", 1) == "builtin"
    assert registry.default_version("payments") == 1


def test_extends_merges_base_and_new_types(tmp_path: Path) -> None:
    registry = SchemaRegistry.builtin([_write(tmp_path / "ext.json", EXTENSION)])
    merged = next(p for p in registry.packs() if (p.id, p.version) == ("infrastructure", 9))
    base = next(p for p in registry.packs() if (p.id, p.version) == ("infrastructure", 2))
    assert set(base.entity_types) < set(merged.entity_types)
    assert "Queue" in merged.entity_types and "ROUTES_TO" in merged.relation_types
    produces = merged.relation_types["PRODUCES"]
    assert produces.sources == (*base.relation_types["PRODUCES"].sources, "Queue")
    assert produces.targets == base.relation_types["PRODUCES"].targets
    assert produces.intents == base.relation_types["PRODUCES"].intents
    # the base is untouched
    assert "Queue" not in base.entity_types


def test_endpoint_validation_error_names_file(tmp_path: Path) -> None:
    bad = {**json.loads(EXAMPLE.read_text()), "id": "bad"}
    bad["relation_types"]["SETTLES_WITH"]["to"] = ["Nope"]
    path = _write(tmp_path / "bad.json", bad)
    with pytest.raises(PackError) as info:
        SchemaRegistry.builtin([path])
    assert str(path) in str(info.value) and "unknown entity type" in str(info.value)


def test_extension_endpoint_must_exist(tmp_path: Path) -> None:
    ext = {**EXTENSION, "relation_types": {"PRODUCES": {"from": ["Ghost"]}}, "entity_types": {}}
    with pytest.raises(PackError, match="unknown entity type"):
        SchemaRegistry.builtin([_write(tmp_path / "e.json", ext)])


def test_duplicate_version_with_builtin_is_error(tmp_path: Path) -> None:
    dup = {**EXTENSION, "version": 2}
    path = _write(tmp_path / "dup.json", dup)
    with pytest.raises(PackError, match=r"infrastructure v2 is already registered"):
        SchemaRegistry.builtin([path])


def test_extension_cannot_redefine_types(tmp_path: Path) -> None:
    redefine = {**EXTENSION, "entity_types": {"Service": {"required": []}}}
    with pytest.raises(PackError, match="already exists"):
        SchemaRegistry.builtin([_write(tmp_path / "r.json", redefine)])
    change = {**EXTENSION, "relation_types": {"PRODUCES": {"allow_cycles": False}}}
    with pytest.raises(PackError, match="only add"):
        SchemaRegistry.builtin([_write(tmp_path / "c.json", change)])


def test_unreadable_and_unknown_base(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="cannot read"):
        SchemaRegistry.builtin([tmp_path / "missing.json"])
    (tmp_path / "x.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(PackError, match="invalid JSON"):
        SchemaRegistry.builtin([tmp_path / "x.json"])
    unknown = {**EXTENSION, "extends": {"id": "infrastructure", "version": 77}}
    with pytest.raises(PackError, match="unknown pack"):
        SchemaRegistry.builtin([_write(tmp_path / "u.json", unknown)])


def _project(tmp_path: Path, config: str) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".ncp").mkdir()
    (root / ".ncp" / "config.toml").write_text(config, encoding="utf-8")
    (root / "ontology" / "packs").mkdir(parents=True)
    (root / "ontology" / "packs" / "payments.v1.json").write_text(EXAMPLE.read_text())
    return root


CONFIG = (
    '[ontology]\nenabled = true\npacks = ["infrastructure", "payments"]\n'
    'pack_paths = ["ontology/packs/payments.v1.json"]\n'
)


def test_config_pack_paths_resolved_and_wired_into_facade(tmp_path: Path) -> None:
    root = _project(tmp_path, CONFIG)
    config = load_config(cwd=root)
    assert config.ontology.pack_paths == (str(root / "ontology/packs/payments.v1.json"),)
    store = SQLiteStore(root / ".ncp" / "store.db")
    facade = OntologyFacade.from_config(config, store)
    auth = AuthContext(
        principal_id="tester", scope=Scope(tenant_id=DEFAULT_TENANT, namespace="shop"),
        capabilities=frozenset({"read", "write"}),
    )
    result = facade.assert_entity(auth, {
        "entity_type": "Ledger", "canonical_key": "main-ledger", "display_name": "Main ledger",
        "attributes": {"name": "main-ledger"}, "schema_pack": "payments",
    })
    assert result["ok"] and result["entity"]["entity_type"] == "Ledger"  # type: ignore[index]


def test_config_pack_paths_from_env(tmp_path: Path) -> None:
    root = _project(tmp_path, "[ontology]\nenabled = true\n")
    config = load_config(cwd=root, env={"NCP_ONTOLOGY_PACK_PATHS": "a.json:/abs/b.json"})
    assert config.ontology.pack_paths == (str(root / "a.json"), "/abs/b.json")


def test_custom_pack_must_be_listed_in_packs(tmp_path: Path) -> None:
    root = _project(tmp_path, CONFIG.replace('"infrastructure", "payments"', '"infrastructure"'))
    facade = OntologyFacade.from_config(load_config(cwd=root), SQLiteStore(root / ".ncp" / "s.db"))
    auth = AuthContext(
        principal_id="t", scope=Scope(tenant_id=DEFAULT_TENANT, namespace="shop"),
        capabilities=frozenset({"read", "write"}),
    )
    from ncp.ontology.facade import OntologyError
    with pytest.raises(OntologyError):
        facade.assert_entity(auth, {
            "entity_type": "Ledger", "canonical_key": "l", "display_name": "l",
            "attributes": {"name": "l"}, "schema_pack": "payments",
        })


def test_config_naming_unregistered_pack_errors(tmp_path: Path) -> None:
    root = _project(tmp_path, '[ontology]\nenabled = true\npacks = ["infrastructure", "payments"]\n')
    with pytest.raises(ValueError, match="unregistered"):
        OntologyFacade.from_config(load_config(cwd=root), SQLiteStore(root / ".ncp" / "s.db"))


class TestPackCli:
    def test_list(self, tmp_path: Path) -> None:
        root = _project(tmp_path, CONFIG + "pack_versions = { infrastructure = 2 }\n")
        result = CliRunner().invoke(main, ["ontology", "pack", "list", "--cwd", str(root), "--json"])
        assert result.exit_code == 0, result.output
        rows = {(r["id"], r["version"]): r for r in json.loads(result.output)}
        assert rows[("infrastructure", 1)]["source"] == "builtin"
        assert rows[("infrastructure", 2)]["pinned"] is True
        assert rows[("infrastructure", 1)]["pinned"] is False
        payments = rows[("payments", 1)]
        assert payments["active"] and payments["entity_types"] == 3 and payments["relation_types"] == 2
        assert payments["source"].endswith("payments.v1.json")
        text = CliRunner().invoke(main, ["ontology", "pack", "list", "--cwd", str(root)])
        assert "payments v1" in text.output and "active" in text.output

    def test_validate_ok_and_error(self, tmp_path: Path) -> None:
        ok = CliRunner().invoke(main, ["ontology", "pack", "validate", str(EXAMPLE)])
        assert ok.exit_code == 0 and "PaymentProvider" in ok.output and "WRITES_LEDGER" in ok.output
        bad = _write(tmp_path / "bad.json", {"id": "x", "version": 1, "entity_types": {}})
        result = CliRunner().invoke(main, ["ontology", "pack", "validate", str(EXAMPLE), str(bad)])
        assert result.exit_code == 1 and str(bad) in result.output

    def test_validate_extension_after_base(self, tmp_path: Path) -> None:
        ext = _write(tmp_path / "ext.json", EXTENSION)
        result = CliRunner().invoke(main, ["ontology", "pack", "validate", str(ext)])
        assert result.exit_code == 0 and "infrastructure v9" in result.output

    def test_show(self, tmp_path: Path) -> None:
        root = _project(tmp_path, CONFIG)
        result = CliRunner().invoke(
            main, ["ontology", "pack", "show", "payments", "--cwd", str(root)],
        )
        assert result.exit_code == 0, result.output
        shown = json.loads(result.output)
        assert shown["id"] == "payments" and "from" in shown["relation_types"]["SETTLES_WITH"]
        missing = CliRunner().invoke(
            main, ["ontology", "pack", "show", "payments", "--version", "5", "--cwd", str(root)],
        )
        assert missing.exit_code != 0
