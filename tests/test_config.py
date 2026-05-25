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
