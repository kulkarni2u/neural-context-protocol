"""Deprecation registry + doctor stale-config checks, warn-once embedding fallback, global --cwd."""

from __future__ import annotations

import json
import logging
import tomllib
import warnings
from pathlib import Path

import pytest
from click.testing import CliRunner

import ncp.deprecations as dep
from ncp.cli import main
from ncp.config import NCPConfig

# ── deprecations registry ─────────────────────────────────────────────────────


def test_unknown_config_keys_flags_typos_not_known_sections() -> None:
    raw = {
        "store": {"type": "sqlite", "pth": "x"},
        "ontology": {"enabled": False, "anything": 1},
        "context_policies": {"p": {}},
        "decision_schemas": {"s": {}},
        "providers": {"pricing": {"foo": 1}},
        "principals": {"a": {}},
        "bogus": {"x": 1},
    }
    assert dep.unknown_config_keys(raw) == ["bogus", "store.pth"]


def test_shipped_config_template_has_no_unknown_keys() -> None:
    template = Path(dep.__file__).parent / "templates" / "config.toml.example"
    raw = tomllib.loads(template.read_text())
    assert dep.unknown_config_keys(raw) == []


def test_deprecated_config_usage_matches_sections_and_keys() -> None:
    removed = (dep.Deprecation("old_section", "config", since="1.0", replacement="new"),)
    deprecated = (
        dep.Deprecation("budget.old_key", "config", since="1.1", removal="2.0"),
        dep.Deprecation("ncp.some_module", "module", since="1.1", removal="2.0"),
    )
    values = {"old_section": {}, "budget": {"old_key": 1}}
    hits = dep.deprecated_config_usage(values, removed=removed, deprecated=deprecated)
    assert [(label, d.name) for label, d in hits] == [("removed", "old_section"), ("deprecated", "budget.old_key")]
    assert dep.deprecated_config_usage({"budget": {}}, removed=removed, deprecated=deprecated) == []


def test_skill_cache_is_not_listed_as_removed() -> None:
    assert all("skill_cache" not in d.name for d in (*dep.REMOVED, *dep.DEPRECATED))


def test_warn_deprecated_emits_once() -> None:
    dep._warned.discard("thing")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dep.warn_deprecated("thing", since="1.0", removal="2.0", replacement="other")
        dep.warn_deprecated("thing", since="1.0", removal="2.0", replacement="other")
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    assert "thing" in str(caught[0].message) and "other" in str(caught[0].message)


# ── doctor ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"])
    assert result.exit_code == 0, result.output
    return tmp_path


def _doctor_checks(*args: str) -> dict[str, dict[str, str]]:
    result = CliRunner().invoke(main, [*args, "doctor", "--port", "1", "--json"])
    assert result.exit_code == 0, result.output
    return {c["name"]: c for c in json.loads(result.output)["checks"]}


def test_doctor_stale_config_ok_for_fresh_init(project: Path) -> None:
    checks = _doctor_checks("--cwd", str(project))
    assert checks["config keys"]["status"] == "ok"


def test_doctor_reports_unknown_config_keys(project: Path) -> None:
    cfg = project / ".ncp" / "config.toml"
    cfg.write_text(cfg.read_text() + "\n[mystery]\nx = 1\n\n[retention]\nnot_a_key = 1\n")
    check = _doctor_checks("--cwd", str(project))["config keys"]
    assert check["status"] == "warn"
    assert "mystery" in check["detail"] and "retention.not_a_key" in check["detail"]
    assert check["fix"]


def test_doctor_reports_removed_and_deprecated_entries(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dep, "REMOVED", (dep.Deprecation("legacy", "config", since="1.8.0", replacement="use [store]"),))
    monkeypatch.setattr(
        dep, "DEPRECATED", (dep.Deprecation("retention.old", "config", since="1.9.0", removal="2.0.0"),)
    )
    cfg = project / ".ncp" / "config.toml"
    cfg.write_text(cfg.read_text() + "\n[legacy]\nx = 1\n\n[retention]\nold = 1\n")
    checks = _doctor_checks("--cwd", str(project))
    assert checks["removed config: legacy"]["status"] == "warn"
    assert "1.8.0" in checks["removed config: legacy"]["detail"]
    assert checks["deprecated config: retention.old"]["status"] == "warn"
    assert "2.0.0" in checks["deprecated config: retention.old"]["detail"]
    # Registered entries are not double-reported as unknown keys.
    assert "legacy" not in checks.get("config keys", {}).get("detail", "")


# ── embeddings warning once ───────────────────────────────────────────────────


def test_local_embedding_fallback_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from ncp.adapters import embedding
    from ncp.stores import factory

    def boom(self: object, model: str = "") -> None:
        raise ImportError("fastembed is required for [embedding].provider = 'local'.")

    monkeypatch.setattr(embedding.LocalEmbeddingAdapter, "__init__", boom)
    monkeypatch.setattr(factory, "_warned_embedding_providers", set())
    config = NCPConfig(
        values={
            "store": {"type": "sqlite", "path": str(tmp_path / "s.db")},
            "embedding": {"enabled": True, "provider": "local", "model": "m"},
            "providers": {"pricing": {}},
        },
        project_root=tmp_path,
    )
    with caplog.at_level(logging.WARNING, logger="ncp"):
        assert factory._build_embedding_adapter(config) is None
        assert factory._build_embedding_adapter(config) is None
    warnings_seen = [r for r in caplog.records if "Semantic retrieval disabled" in r.getMessage()]
    assert len(warnings_seen) == 1


# ── global --cwd ──────────────────────────────────────────────────────────────


def test_global_cwd_is_used_by_subcommand(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project.parent)
    result = CliRunner().invoke(main, ["--cwd", str(project), "doctor", "--port", "1", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert checks["config"]["status"] == "ok"
    assert str(project) in checks["config"]["detail"]


def test_subcommand_cwd_overrides_global(project: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    empty = tmp_path_factory.mktemp("empty")
    result = CliRunner().invoke(
        main, ["--cwd", str(project), "doctor", "--cwd", str(empty), "--port", "1", "--json"]
    )
    checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert checks["config"]["status"] == "warn"  # the empty dir has no config.toml


def test_without_global_cwd_default_is_process_cwd(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    result = CliRunner().invoke(main, ["doctor", "--port", "1", "--json"])
    checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert checks["config"]["status"] == "ok"


def test_global_cwd_applies_to_default_none_and_nested_groups(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import click

    from ncp import cli

    seen: dict[str, object] = {}

    @cli.main.command("_probe_none")
    @click.option("--cwd", type=click.Path(path_type=Path), default=None)
    def probe_none(cwd: Path | None) -> None:
        seen["none"] = cwd

    @cli.main.group("_probe_group")
    def probe_group() -> None: ...

    @probe_group.command("sub")
    @click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
    def probe_sub(cwd: Path) -> None:
        seen["nested"] = cwd

    try:
        runner = CliRunner()
        assert runner.invoke(cli.main, ["--cwd", str(project), "_probe_none"]).exit_code == 0
        assert runner.invoke(cli.main, ["--cwd", str(project), "_probe_group", "sub"]).exit_code == 0
        assert seen == {"none": project, "nested": project}
        # Explicit subcommand --cwd wins, including in nested groups.
        runner.invoke(cli.main, ["--cwd", str(project), "_probe_group", "sub", "--cwd", "/x"])
        assert seen["nested"] == Path("/x")
    finally:
        cli.main.commands.pop("_probe_none", None)
        cli.main.commands.pop("_probe_group", None)


def test_real_nested_group_honors_global_cwd(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project.parent)
    result = CliRunner().invoke(main, ["--cwd", str(project), "identity", "list"])
    assert result.exit_code == 0, result.output


def test_global_cwd_rejects_missing_dir(project: Path) -> None:
    result = CliRunner().invoke(main, ["--cwd", str(project / "nope"), "doctor"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_global_cwd_rejects_file(project: Path) -> None:
    result = CliRunner().invoke(main, ["--cwd", str(project / ".ncp" / "config.toml"), "doctor"])
    assert result.exit_code != 0
    assert "is a file" in result.output
