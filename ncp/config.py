"""Config loading and override resolution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


DEFAULT_CONFIG = {
    "store": {
        "type": "sqlite",
        "path": ".ncp/store.db",
    },
    "redis": {
        "url": "redis://127.0.0.1:6379/0",
        "stream": "ncp:whispers",
    },
    "pgvector": {
        "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        "schema": "ncp",
        "table_prefix": "ncp_",
    },
    "pipeline": {
        "default_ttl_hours": 24,
        "max_working_chunks": 500,
        "gc_threshold": 400,
        "cold_start_retry": 2,
    },
    "budget": {
        "max_tokens_per_call": 4000,
        "warn_at_ratio": 0.70,
        "critical_at_ratio": 0.85,
    },
    "chunking": {
        "max_chunk_tokens": 200,
        "default_type": "auto",
    },
    "whispers": {
        "default_ttl_seconds": 60,
        "max_per_drain": 3,
        "min_confidence": 0.60,
    },
    "observability": {
        "log_level": "info",
        "log_format": "pretty",
        "cost_tracking": True,
    },
    "retrieval": {
        "rerank_enabled": False,
        "rerank_provider": "local",
        "rerank_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    },
    "embedding": {
        "enabled": False,
        "provider": "local",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
    },
    "consolidation": {
        "enabled": True,
        "similarity_threshold": 0.65,
        "trust_floor": 0.10,
        "model_provider": None,
        "model": None,
    },
    "providers": {
        "pricing": {
            "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
            "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "cache_read": 0.08},
            "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
            "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
        }
    },
}


@dataclass(slots=True)
class NCPConfig:
    """Normalized NCP configuration."""

    values: dict[str, Any]
    project_root: Path

    @property
    def store_type(self) -> str:
        return str(self.values["store"]["type"])

    @property
    def store_path(self) -> Path:
        path = Path(str(self.values["store"]["path"]))
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def pricing(self) -> dict[str, dict[str, float]]:
        return dict(self.values.get("providers", {}).get("pricing", {}))

    @property
    def redis_url(self) -> str:
        return str(self.values.get("redis", {}).get("url", ""))

    @property
    def redis_stream(self) -> str:
        return str(self.values.get("redis", {}).get("stream", "ncp:whispers"))

    @property
    def pgvector_dsn(self) -> str:
        return str(self.values.get("pgvector", {}).get("dsn", ""))

    @property
    def pgvector_schema(self) -> str:
        return str(self.values.get("pgvector", {}).get("schema", "ncp"))

    @property
    def pgvector_table_prefix(self) -> str:
        return str(self.values.get("pgvector", {}).get("table_prefix", "ncp_"))

    @property
    def consolidation_enabled(self) -> bool:
        return bool(self.values.get("consolidation", {}).get("enabled", True))

    @property
    def consolidation_similarity_threshold(self) -> float:
        return float(self.values.get("consolidation", {}).get("similarity_threshold", 0.65))

    @property
    def consolidation_trust_floor(self) -> float:
        return float(self.values.get("consolidation", {}).get("trust_floor", 0.10))

    @property
    def consolidation_model_provider(self) -> str | None:
        val = self.values.get("consolidation", {}).get("model_provider")
        return str(val) if val else None

    @property
    def consolidation_model(self) -> str | None:
        val = self.values.get("consolidation", {}).get("model")
        return str(val) if val else None

    @property
    def rerank_enabled(self) -> bool:
        return bool(self.values.get("retrieval", {}).get("rerank_enabled", False))

    @property
    def rerank_provider(self) -> str:
        return str(self.values.get("retrieval", {}).get("rerank_provider", "local"))

    @property
    def rerank_model(self) -> str | None:
        val = self.values.get("retrieval", {}).get("rerank_model")
        return str(val) if val else None

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.values.get("embedding", {}).get("enabled", False))

    @property
    def embedding_provider(self) -> str:
        return str(self.values.get("embedding", {}).get("provider", "local"))

    @property
    def embedding_model(self) -> str:
        return str(self.values.get("embedding", {}).get("model", "sentence-transformers/all-MiniLM-L6-v2"))

def load_config(
    path: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> NCPConfig:
    """Load config from defaults, TOML, and environment overrides."""

    runtime_env = os.environ if env is None else env
    current_dir = Path.cwd() if cwd is None else Path(cwd)
    project_root = find_project_root(current_dir)
    config_path = Path(path) if path is not None else project_root / ".ncp" / "config.toml"

    values = _deep_copy(DEFAULT_CONFIG)
    if config_path.exists():
        with config_path.open("rb") as handle:
            file_values = tomllib.load(handle)
        _deep_merge(values, file_values)

    _apply_env_overrides(values, runtime_env)
    store_type = str(values["store"]["type"])
    if store_type == "redis":
        raise NotImplementedError(
            f"Store type '{store_type}' is accepted for forward compatibility but not implemented in V1."
        )
    if store_type not in {"sqlite", "pgvector"}:
        raise ValueError(f"Unsupported store type: {store_type}")

    if not Path(str(values["store"]["path"])).is_absolute():
        values["store"]["path"] = str(project_root / str(values["store"]["path"]))

    return NCPConfig(values=values, project_root=project_root)


def find_project_root(start: str | Path) -> Path:
    """Find the nearest project root by walking up to a ``.git`` directory."""

    current = Path(start).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _apply_env_overrides(values: dict[str, Any], env: dict[str, str]) -> None:
    if "NCP_STORE_PATH" in env:
        values["store"]["path"] = env["NCP_STORE_PATH"]
    if "NCP_LOG_LEVEL" in env:
        values["observability"]["log_level"] = env["NCP_LOG_LEVEL"]
    if "NCP_STORE_TYPE" in env:
        values["store"]["type"] = env["NCP_STORE_TYPE"]
    if "NCP_REDIS_URL" in env:
        values["redis"]["url"] = env["NCP_REDIS_URL"]
    if "NCP_REDIS_STREAM" in env:
        values["redis"]["stream"] = env["NCP_REDIS_STREAM"]
    if "NCP_PGVECTOR_DSN" in env:
        values["pgvector"]["dsn"] = env["NCP_PGVECTOR_DSN"]
    if "NCP_PGVECTOR_SCHEMA" in env:
        values["pgvector"]["schema"] = env["NCP_PGVECTOR_SCHEMA"]
    if "NCP_PGVECTOR_TABLE_PREFIX" in env:
        values["pgvector"]["table_prefix"] = env["NCP_PGVECTOR_TABLE_PREFIX"]
    if "NCP_RERANK_ENABLED" in env:
        val = env["NCP_RERANK_ENABLED"].lower()
        values["retrieval"]["rerank_enabled"] = val in {"true", "1", "yes"}
    if "NCP_RERANK_PROVIDER" in env:
        values["retrieval"]["rerank_provider"] = env["NCP_RERANK_PROVIDER"]
    if "NCP_RERANK_MODEL" in env:
        values["retrieval"]["rerank_model"] = env["NCP_RERANK_MODEL"]
    if "NCP_EMBEDDING_ENABLED" in env:
        val = env["NCP_EMBEDDING_ENABLED"].lower()
        values["embedding"]["enabled"] = val in {"true", "1", "yes"}
    if "NCP_EMBEDDING_PROVIDER" in env:
        values["embedding"]["provider"] = env["NCP_EMBEDDING_PROVIDER"]
    if "NCP_EMBEDDING_MODEL" in env:
        values["embedding"]["model"] = env["NCP_EMBEDDING_MODEL"]


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _deep_copy(value: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        copied[key] = _deep_copy(item) if isinstance(item, dict) else item
    return copied
