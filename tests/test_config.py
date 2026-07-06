from pathlib import Path

import pytest

from ncp.config import find_project_root, load_config


def test_load_config_uses_project_local_default_store_path(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    config = load_config(cwd=project)

    assert config.store_type == "sqlite"
    assert config.store_path == project / ".ncp" / "store.db"


def test_load_config_reads_file_and_env_overrides(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[store]\npath = \".ncp/custom.db\"\n\n[observability]\nlog_level = \"debug\"\n"
    )

    config = load_config(
        cwd=project,
        env={"NCP_STORE_PATH": "/tmp/override.db", "NCP_LOG_LEVEL": "warning"},
    )

    assert config.store_path == Path("/tmp/override.db")
    assert config.values["observability"]["log_level"] == "warning"


def test_load_config_exposes_redis_and_pgvector_settings(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[redis]\nurl = \"redis://127.0.0.1:6380/1\"\nstream = \"ncp:test\"\n\n"
        "[pgvector]\ndsn = \"postgresql://postgres:postgres@127.0.0.1:5433/ncp_test\"\n"
        "schema = \"ncp_test\"\ntable_prefix = \"demo_\"\n"
    )

    config = load_config(
        cwd=project,
        env={
            "NCP_REDIS_URL": "redis://127.0.0.1:6390/5",
            "NCP_PGVECTOR_DSN": "postgresql://postgres:postgres@127.0.0.1:5440/ncp_override",
            "NCP_PGVECTOR_SCHEMA": "ncp_override",
            "NCP_PGVECTOR_TABLE_PREFIX": "override_",
        },
    )

    assert config.redis_url == "redis://127.0.0.1:6390/5"
    assert config.redis_stream == "ncp:test"
    assert config.pgvector_dsn == "postgresql://postgres:postgres@127.0.0.1:5440/ncp_override"
    assert config.pgvector_schema == "ncp_override"
    assert config.pgvector_table_prefix == "override_"


def test_load_config_allows_pgvector_store_type(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    config = load_config(cwd=project, env={"NCP_STORE_TYPE": "pgvector"})

    assert config.store_type == "pgvector"


def test_load_config_rejects_redis_store_type(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    with pytest.raises(NotImplementedError, match="forward compatibility"):
        load_config(cwd=project, env={"NCP_STORE_TYPE": "redis"})


def test_load_config_rejects_unknown_store_type(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    with pytest.raises(ValueError, match="Unsupported store type"):
        load_config(cwd=project, env={"NCP_STORE_TYPE": "mystery"})


def test_find_project_root_walks_up_tree(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    nested = project / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()

    assert find_project_root(nested) == project


def test_embedding_config_defaults(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(cwd=project)
    assert config.embedding_enabled is False
    assert config.embedding_provider == "local"
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"


def test_embedding_config_toml_override(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[embedding]\nenabled = true\nprovider = \"openai\"\nmodel = \"text-embedding-3-small\"\n"
    )
    config = load_config(cwd=project)
    assert config.embedding_enabled is True
    assert config.embedding_provider == "openai"
    assert config.embedding_model == "text-embedding-3-small"


def test_embedding_config_env_overrides(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(
        cwd=project,
        env={
            "NCP_EMBEDDING_ENABLED": "true",
            "NCP_EMBEDDING_PROVIDER": "openai",
            "NCP_EMBEDDING_MODEL": "text-embedding-3-small",
        },
    )
    assert config.embedding_enabled is True
    assert config.embedding_provider == "openai"
    assert config.embedding_model == "text-embedding-3-small"


def test_retrieval_diversity_lambda_defaults_and_overrides(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()

    assert load_config(cwd=project).diversity_lambda == 1.0

    (project / ".ncp" / "config.toml").write_text("[retrieval]\ndiversity_lambda = 0.7\n")
    assert load_config(cwd=project).diversity_lambda == pytest.approx(0.7)

    config = load_config(cwd=project, env={"NCP_DIVERSITY_LAMBDA": "0.8"})
    assert config.diversity_lambda == pytest.approx(0.8)


def test_distillation_config_defaults_and_overrides(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()

    config = load_config(cwd=project)
    assert config.distillation_enabled is False
    assert config.distillation_min_chunk_tokens == 120

    (project / ".ncp" / "config.toml").write_text(
        "[distillation]\nenabled = true\nmin_chunk_tokens = 32\n"
    )
    config = load_config(cwd=project)
    assert config.distillation_enabled is True
    assert config.distillation_min_chunk_tokens == 32

    config = load_config(
        cwd=project,
        env={
            "NCP_DISTILLATION_ENABLED": "false",
            "NCP_DISTILLATION_MIN_CHUNK_TOKENS": "64",
        },
    )
    assert config.distillation_enabled is False
    assert config.distillation_min_chunk_tokens == 64


def test_budget_governor_config_defaults(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    config = load_config(cwd=project)

    assert config.pipeline_budget_usd is None
    assert config.budget_warn_fraction == pytest.approx(0.8)
    assert config.budget_enforcement == "warn"


def test_budget_governor_config_file_and_env_overrides(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[budget]\npipeline_budget_usd = 5.0\nbudget_warn_fraction = 0.5\n"
        "budget_enforcement = \"block\"\n"
    )

    config = load_config(cwd=project)
    assert config.pipeline_budget_usd == pytest.approx(5.0)
    assert config.budget_warn_fraction == pytest.approx(0.5)
    assert config.budget_enforcement == "block"

    config = load_config(
        cwd=project,
        env={
            "NCP_PIPELINE_BUDGET_USD": "12.5",
            "NCP_BUDGET_WARN_FRACTION": "0.9",
            "NCP_BUDGET_ENFORCEMENT": "off",
        },
    )
    assert config.pipeline_budget_usd == pytest.approx(12.5)
    assert config.budget_warn_fraction == pytest.approx(0.9)
    assert config.budget_enforcement == "off"


def test_budget_governor_env_override_can_clear_pipeline_budget(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text("[budget]\npipeline_budget_usd = 5.0\n")

    config = load_config(cwd=project, env={"NCP_PIPELINE_BUDGET_USD": ""})
    assert config.pipeline_budget_usd is None


def test_budget_enforcement_falls_back_to_warn_for_unknown_value(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[budget]\nbudget_enforcement = \"nonsense\"\n"
    )

    config = load_config(cwd=project)
    assert config.budget_enforcement == "warn"


def test_adaptive_budget_config_defaults(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    config = load_config(cwd=project)

    assert config.adaptive_budget_enabled is False
    assert config.adaptive_budget_floor_tokens == 300
    assert config.adaptive_budget_ceiling_tokens == 2000


def test_adaptive_budget_config_file_and_env_overrides(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[budget]\nadaptive_budget_enabled = true\n"
        "adaptive_budget_floor_tokens = 250\n"
        "adaptive_budget_ceiling_tokens = 1500\n"
    )

    config = load_config(cwd=project)
    assert config.adaptive_budget_enabled is True
    assert config.adaptive_budget_floor_tokens == 250
    assert config.adaptive_budget_ceiling_tokens == 1500

    config = load_config(
        cwd=project,
        env={
            "NCP_ADAPTIVE_BUDGET_ENABLED": "false",
            "NCP_ADAPTIVE_BUDGET_FLOOR_TOKENS": "400",
            "NCP_ADAPTIVE_BUDGET_CEILING_TOKENS": "1800",
        },
    )
    assert config.adaptive_budget_enabled is False
    assert config.adaptive_budget_floor_tokens == 400
    assert config.adaptive_budget_ceiling_tokens == 1800


def test_tier_hints_enabled_config_default_and_overrides(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    assert load_config(cwd=project).tier_hints_enabled is True

    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text("[tiering]\ntier_hints_enabled = false\n")
    config = load_config(cwd=project)
    assert config.tier_hints_enabled is False

    config = load_config(cwd=project, env={"NCP_TIER_HINTS_ENABLED": "true"})
    assert config.tier_hints_enabled is True


def test_drift_config_defaults(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    config = load_config(cwd=project)

    assert config.drift_computed_enabled is False
    assert config.drift_window_turns == 5
    assert config.drift_use_embeddings is False


def test_drift_config_file_and_env_overrides(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[drift]\ndrift_computed_enabled = true\n"
        "drift_window_turns = 8\n"
        "drift_use_embeddings = true\n"
    )

    config = load_config(cwd=project)
    assert config.drift_computed_enabled is True
    assert config.drift_window_turns == 8
    assert config.drift_use_embeddings is True

    config = load_config(
        cwd=project,
        env={
            "NCP_DRIFT_COMPUTED_ENABLED": "false",
            "NCP_DRIFT_WINDOW_TURNS": "3",
            "NCP_DRIFT_USE_EMBEDDINGS": "false",
        },
    )
    assert config.drift_computed_enabled is False
    assert config.drift_window_turns == 3
    assert config.drift_use_embeddings is False


def test_drift_window_turns_is_clamped_to_at_least_one(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text("[drift]\ndrift_window_turns = 0\n")

    config = load_config(cwd=project)
    assert config.drift_window_turns == 1
