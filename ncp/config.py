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
        "context_token_budget": 840,
        "warn_at_ratio": 0.70,
        "critical_at_ratio": 0.85,
        "chunk_cap_default": 4,
        "chunk_cap_high": 3,
        "chunk_cap_critical": 2,
        "recent_slot_budget": 2,
        "whisper_cap_default": 3,
        "whisper_cap_high": 2,
        "whisper_cap_critical": 1,
        # CAP-E2: per-pipeline $ budget governance over real (CAP-E1) cost
        # accounting. None (default) disables the governor entirely.
        "pipeline_budget_usd": None,
        "budget_warn_fraction": 0.8,
        "budget_enforcement": "warn",
    },
    "chunking": {
        "max_chunk_tokens": 200,
        "default_type": "auto",
    },
    "whispers": {
        "default_ttl_seconds": 1800,
        "max_per_drain": 3,
        "min_confidence": 0.60,
        "min_author_reputation": 0.0,
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
        "generation_penalty_base": 0.9,
        "edge_expansion": True,
        "edge_expansion_decay": 0.7,
        "trust_propagation_factor": 0.5,
        "dissent_weight": 0.2,
        "diversity_lambda": 1.0,
        "usage_prior_weight": 1.0,
        "reputation_weight": 0.0,
    },
    "reputation": {
        "gain": 4.0,
        "forget": 0.99,
        "confidence_k": 20,
    },
    "embedding": {
        "enabled": False,
        "provider": "local",
        "model": "BAAI/bge-small-en-v1.5",
    },
    "distillation": {
        "enabled": False,
        "min_chunk_tokens": 120,
    },
    "consolidation": {
        "enabled": True,
        "similarity_threshold": 0.65,
        "trust_floor": 0.10,
        "model_provider": None,
        "model": None,
    },
    "retention": {
        "max_working_chunks_per_pipeline": 0,
    },
    "server": {
        "auth_token": "",
    },
    "memoization": {
        "enabled": False,
        "max_age_hours": 24,
        "min_outcome": 0.0,
        "allow_unverified": False,
        "similarity_threshold": 0.95,
    },
    "identity": {
        # OPT-IN authorship enforcement. When false (default) unsigned writes and
        # whispers keep working exactly as before and any supplied signature is
        # verified and recorded but never required. When true, a write/emit whose
        # authorship cannot be verified (missing/bad signature or revoked identity)
        # is rejected.
        "require_signatures": False,
    },
    "tiering": {
        # CAP-E3: advisory model-tiering signal on ncp_get_context responses.
        # NCP never routes models itself; this only emits a signal an
        # orchestrator can use. Default true; set false to omit the fields.
        "tier_hints_enabled": True,
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
    def retrieval_generation_penalty_base(self) -> float:
        return float(self.values.get("retrieval", {}).get("generation_penalty_base", 0.9))

    @property
    def edge_expansion_enabled(self) -> bool:
        return bool(self.values.get("retrieval", {}).get("edge_expansion", True))

    @property
    def edge_expansion_decay(self) -> float:
        return float(self.values.get("retrieval", {}).get("edge_expansion_decay", 0.7))

    @property
    def trust_propagation_factor(self) -> float:
        return float(self.values.get("retrieval", {}).get("trust_propagation_factor", 0.5))

    @property
    def dissent_weight(self) -> float:
        return float(self.values.get("retrieval", {}).get("dissent_weight", 0.2))

    @property
    def diversity_lambda(self) -> float:
        return float(self.values.get("retrieval", {}).get("diversity_lambda", 1.0))

    @property
    def usage_prior_weight(self) -> float:
        return float(self.values.get("retrieval", {}).get("usage_prior_weight", 1.0))

    @property
    def reputation_weight(self) -> float:
        return float(self.values.get("retrieval", {}).get("reputation_weight", 0.0))

    @property
    def reputation_gain(self) -> float:
        return float(self.values.get("reputation", {}).get("gain", 4.0))

    @property
    def reputation_forget(self) -> float:
        return float(self.values.get("reputation", {}).get("forget", 0.99))

    @property
    def reputation_confidence_k(self) -> int:
        return int(self.values.get("reputation", {}).get("confidence_k", 20))

    @property
    def context_token_budget(self) -> int:
        return int(self.values.get("budget", {}).get("context_token_budget", 840))

    @property
    def chunk_cap_default(self) -> int:
        return int(self.values.get("budget", {}).get("chunk_cap_default", 4))

    @property
    def chunk_cap_high(self) -> int:
        return int(self.values.get("budget", {}).get("chunk_cap_high", 3))

    @property
    def chunk_cap_critical(self) -> int:
        return int(self.values.get("budget", {}).get("chunk_cap_critical", 2))

    @property
    def recent_slot_budget(self) -> int:
        return int(self.values.get("budget", {}).get("recent_slot_budget", 2))

    @property
    def whisper_cap_default(self) -> int:
        return int(self.values.get("budget", {}).get("whisper_cap_default", 3))

    @property
    def whisper_cap_high(self) -> int:
        return int(self.values.get("budget", {}).get("whisper_cap_high", 2))

    @property
    def whisper_cap_critical(self) -> int:
        return int(self.values.get("budget", {}).get("whisper_cap_critical", 1))

    @property
    def pipeline_budget_usd(self) -> float | None:
        """CAP-E2: per-pipeline $ spend ceiling. None (default) disables the governor."""
        val = self.values.get("budget", {}).get("pipeline_budget_usd")
        return None if val is None else float(val)

    @property
    def budget_warn_fraction(self) -> float:
        return float(self.values.get("budget", {}).get("budget_warn_fraction", 0.8))

    @property
    def budget_enforcement(self) -> str:
        """CAP-E2 enforcement mode: 'off' | 'warn' | 'block'. Falls back to 'warn' for unknown values."""
        val = str(self.values.get("budget", {}).get("budget_enforcement", "warn"))
        return val if val in {"off", "warn", "block"} else "warn"

    @property
    def tier_hints_enabled(self) -> bool:
        """CAP-E3: whether ncp_get_context emits the advisory tier_hint/complexity_signal fields."""
        return bool(self.values.get("tiering", {}).get("tier_hints_enabled", True))

    @property
    def whisper_ttl_default(self) -> int:
        return int(self.values.get("whispers", {}).get("default_ttl_seconds", 1800))

    @property
    def whisper_min_author_reputation(self) -> float:
        return float(self.values.get("whispers", {}).get("min_author_reputation", 0.0))

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.values.get("embedding", {}).get("enabled", False))

    @property
    def embedding_provider(self) -> str:
        return str(self.values.get("embedding", {}).get("provider", "local"))

    @property
    def embedding_model(self) -> str:
        return str(self.values.get("embedding", {}).get("model", "BAAI/bge-small-en-v1.5"))

    @property
    def distillation_enabled(self) -> bool:
        return bool(self.values.get("distillation", {}).get("enabled", False))

    @property
    def distillation_min_chunk_tokens(self) -> int:
        return int(self.values.get("distillation", {}).get("min_chunk_tokens", 120))

    @property
    def server_auth_token(self) -> str | None:
        val = self.values.get("server", {}).get("auth_token")
        return str(val) if val else None

    @property
    def retention_max_working_chunks_per_pipeline(self) -> int:
        return int(self.values.get("retention", {}).get("max_working_chunks_per_pipeline", 0))

    @property
    def memoization_enabled(self) -> bool:
        return bool(self.values.get("memoization", {}).get("enabled", False))

    @property
    def memoization_max_age_hours(self) -> int:
        return int(self.values.get("memoization", {}).get("max_age_hours", 24))

    @property
    def memoization_min_outcome(self) -> float:
        return float(self.values.get("memoization", {}).get("min_outcome", 0.0))

    @property
    def memoization_allow_unverified(self) -> bool:
        return bool(self.values.get("memoization", {}).get("allow_unverified", False))

    @property
    def memoization_similarity_threshold(self) -> float:
        return float(self.values.get("memoization", {}).get("similarity_threshold", 0.95))

    @property
    def require_signatures(self) -> bool:
        return bool(self.values.get("identity", {}).get("require_signatures", False))

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
    if "NCP_DISTILLATION_ENABLED" in env:
        val = env["NCP_DISTILLATION_ENABLED"].lower()
        values["distillation"]["enabled"] = val in {"true", "1", "yes"}
    if "NCP_DISTILLATION_MIN_CHUNK_TOKENS" in env:
        values["distillation"]["min_chunk_tokens"] = int(env["NCP_DISTILLATION_MIN_CHUNK_TOKENS"])
    if "NCP_GENERATION_PENALTY_BASE" in env:
        values["retrieval"]["generation_penalty_base"] = float(env["NCP_GENERATION_PENALTY_BASE"])
    if "NCP_EDGE_EXPANSION" in env:
        val = env["NCP_EDGE_EXPANSION"].lower()
        values["retrieval"]["edge_expansion"] = val in {"true", "1", "yes"}
    if "NCP_TRUST_PROPAGATION_FACTOR" in env:
        values["retrieval"]["trust_propagation_factor"] = float(env["NCP_TRUST_PROPAGATION_FACTOR"])
    if "NCP_DISSENT_WEIGHT" in env:
        values["retrieval"]["dissent_weight"] = float(env["NCP_DISSENT_WEIGHT"])
    if "NCP_DIVERSITY_LAMBDA" in env:
        values["retrieval"]["diversity_lambda"] = float(env["NCP_DIVERSITY_LAMBDA"])
    if "NCP_REPUTATION_GAIN" in env:
        values["reputation"]["gain"] = float(env["NCP_REPUTATION_GAIN"])
    if "NCP_REPUTATION_FORGET" in env:
        values["reputation"]["forget"] = float(env["NCP_REPUTATION_FORGET"])
    if "NCP_REPUTATION_CONFIDENCE_K" in env:
        values["reputation"]["confidence_k"] = int(env["NCP_REPUTATION_CONFIDENCE_K"])
    if "NCP_AUTH_TOKEN" in env:
        values["server"]["auth_token"] = env["NCP_AUTH_TOKEN"]
    if "NCP_PIPELINE_BUDGET_USD" in env:
        raw_budget = env["NCP_PIPELINE_BUDGET_USD"]
        values["budget"]["pipeline_budget_usd"] = None if raw_budget == "" else float(raw_budget)
    if "NCP_BUDGET_WARN_FRACTION" in env:
        values["budget"]["budget_warn_fraction"] = float(env["NCP_BUDGET_WARN_FRACTION"])
    if "NCP_BUDGET_ENFORCEMENT" in env:
        values["budget"]["budget_enforcement"] = env["NCP_BUDGET_ENFORCEMENT"]
    if "NCP_TIER_HINTS_ENABLED" in env:
        val = env["NCP_TIER_HINTS_ENABLED"].lower()
        values["tiering"]["tier_hints_enabled"] = val in {"true", "1", "yes"}


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
