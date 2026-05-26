"""Core NCP data models for the first launch-critical slice."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PressureLevel = Literal["low", "medium", "high", "critical"]
ChunkLayer = Literal["episodic", "procedural", "semantic", "social", "reasoning_trace"]
ChunkSource = Literal[
    "user_verified",
    "tool_result",
    "agent_inferred",
    "synthesis",
    "subcon_retrieved",
]
ChunkType = Literal["prose", "json", "code", "table", "auto"]
ChunkScope = Literal["pipeline", "global"]
ChunkZone = Literal["working", "proven", "global"]
WhisperType = Literal[
    "nudge",
    "alert",
    "share",
    "request",
    "dissent",
    "world_check",
    "consolidation_ready",
]


def _validate_no_spaces(value: str, field_name: str) -> str:
    if any(char.isspace() for char in value):
        raise ValueError(f"{field_name} must not contain whitespace")
    return value


def _validate_no_spaces_list(values: list[str], field_name: str) -> list[str]:
    for value in values:
        _validate_no_spaces(value, field_name)
    return values


class NCPModel(BaseModel):
    """Base model that tolerates forward-compatible fields."""

    model_config = ConfigDict(extra="ignore")


class ConsciousBlock(NCPModel):
    """Per-turn conscious state injected into the working context."""

    agent_id: str
    role: str
    owns: list[str]
    must_not: list[str]
    task: str
    slot: str
    intent: str
    ncp_v: str = "1.0"

    slot_age: int = 0
    slot_confidence: float = 1.0
    goal_version: int = 1
    drift_score: float = 0.0
    intent_anchor: str | None = None

    recent: list[str] = Field(default_factory=list)

    tried: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    escalate_to: str | None = None

    ctx_used_ratio: float = 0.0
    ctx_window: int = 200000
    steps_completed: int = 0
    steps_total: int | None = None
    pressure: PressureLevel = "low"

    calibration_id: str | None = None
    pipeline_id: str | None = None

    @field_validator("agent_id", "role", "task", "slot", "intent", "ncp_v")
    @classmethod
    def _no_spaces(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("owns", "must_not", "recent", "tried", "failed")
    @classmethod
    def _no_spaces_in_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces_list(values, field_name)

    @field_validator("ncp_v")
    @classmethod
    def _validate_ncp_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("ncp_v must be '1.0'")
        return value

    @field_validator("slot_age", "goal_version", "ctx_window", "steps_completed")
    @classmethod
    def _non_negative_ints(cls, value: int, info: object) -> int:
        field_name = getattr(info, "field_name", "field")
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0")
        return value

    @field_validator("steps_total")
    @classmethod
    def _steps_total_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("steps_total must be >= 1 when provided")
        return value

    @field_validator("slot_confidence", "drift_score", "ctx_used_ratio")
    @classmethod
    def _validate_unit_interval(cls, value: float, info: object) -> float:
        field_name = getattr(info, "field_name", "field")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{field_name} must be between 0.0 and 1.0")
        return value


class BudgetContext(NCPModel):
    """Assembler-facing budget state for the pidgin budget block."""

    ctx_used: float = 0.0
    steps_completed: int = 0
    steps_total: int | None = None
    elapsed_seconds: float = 0.0
    pressure: PressureLevel = "low"

    @field_validator("ctx_used")
    @classmethod
    def _validate_ctx_used(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("ctx_used must be between 0.0 and 1.0")
        return value

    @field_validator("steps_completed")
    @classmethod
    def _validate_steps_completed(cls, value: int) -> int:
        if value < 0:
            raise ValueError("steps_completed must be >= 0")
        return value

    @field_validator("steps_total")
    @classmethod
    def _validate_steps_total(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("steps_total must be >= 1 when provided")
        return value

    @field_validator("elapsed_seconds")
    @classmethod
    def _validate_elapsed_seconds(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("elapsed_seconds must be >= 0.0")
        return value


class SubconsciousChunk(NCPModel):
    """Stored chunk with provenance, trust, and retrieval metadata."""

    chunk_id: str = Field(default_factory=lambda: f"sub_{uuid4().hex[:12]}")
    layer: ChunkLayer
    content: str
    src: ChunkSource

    written_by: str = "system"
    caused_by: str | None = None
    conscious_hash: str | None = None
    evidence_id: str | None = None

    generation: int = 0
    base_trust: float = 0.7

    result_confidence: float | None = None
    result_attempts: int | None = None

    conditions: list[str] = Field(default_factory=list)
    valid_while: str | None = None
    expiry: float | None = None
    owner: str | None = None

    chunk_type: ChunkType = "prose"

    pipeline_id: str | None = None
    scope: ChunkScope = "pipeline"
    zone: ChunkZone = "working"
    schema_version: int = 1
    supersedes: str | None = None
    source_refs: list[str] = Field(default_factory=list)

    relevance: float = 0.0
    age_seconds: float = 0.0

    @property
    def effective_score(self) -> float:
        """Derived retrieval score used by the assembler."""

        decay = math.exp(-0.693 * self.age_seconds / 14400)
        generation_penalty = 0.9 ** self.generation
        return self.relevance * decay * self.base_trust * generation_penalty

    @field_validator("chunk_id", "written_by")
    @classmethod
    def _chunk_fields_no_spaces(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("conditions", "source_refs")
    @classmethod
    def _chunk_lists_no_spaces(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces_list(values, field_name)

    @field_validator("content")
    @classmethod
    def _content_within_limit(cls, value: str) -> str:
        if len(value) > 2000:
            raise ValueError("content must be <= 2000 characters")
        return value

    @field_validator("generation", "schema_version")
    @classmethod
    def _chunk_non_negative_ints(cls, value: int, info: object) -> int:
        field_name = getattr(info, "field_name", "field")
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0")
        return value

    @field_validator("result_attempts")
    @classmethod
    def _result_attempts_non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("result_attempts must be >= 0 when provided")
        return value

    @field_validator("base_trust", "result_confidence", "relevance")
    @classmethod
    def _chunk_unit_interval(cls, value: float | None, info: object) -> float | None:
        if value is None:
            return value
        field_name = getattr(info, "field_name", "field")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{field_name} must be between 0.0 and 1.0")
        return value

    @field_validator("age_seconds")
    @classmethod
    def _age_seconds_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("age_seconds must be >= 0.0")
        return value

    @model_validator(mode="after")
    def _validate_zone_expiry(self) -> SubconsciousChunk:
        if self.zone in {"proven", "global"} and self.expiry is None:
            raise ValueError("proven/global zones require expiry")
        return self


class Whisper(NCPModel):
    """Short agent-to-agent signal scoped to a pipeline."""

    from_agent: str
    target: str
    whisper_type: WhisperType
    payload: str
    confidence: float

    whisper_id: str = Field(default_factory=lambda: f"wsp_{uuid4().hex[:12]}")
    ref: str | None = None
    created_at: float = Field(default_factory=time.time)
    ttl_seconds: int = 60
    pipeline_id: str | None = None
    dissent_target: str | None = None

    @field_validator("from_agent", "target", "whisper_id")
    @classmethod
    def _whisper_no_spaces(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("ref", "pipeline_id", "dissent_target")
    @classmethod
    def _optional_whisper_no_spaces(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return value
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("payload")
    @classmethod
    def _payload_within_limit(cls, value: str) -> str:
        if len(value) > 600:
            raise ValueError("payload must be <= 600 characters")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("created_at must be >= 0.0")
        return value

    @field_validator("ttl_seconds")
    @classmethod
    def _ttl_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ttl_seconds must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_dissent_target(self) -> Whisper:
        if self.whisper_type == "dissent" and self.target == "*":
            raise ValueError("dissent whispers cannot target '*'")
        return self


class TurnRecord(NCPModel):
    """Stored per-turn summary and fetchable full result."""

    turn_id: str = Field(default_factory=lambda: f"turn_{uuid4().hex[:12]}")
    agent_id: str
    pipeline_id: str | None = None
    task: str
    slot: str
    result: str
    result_full: str
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None

    @field_validator("turn_id", "agent_id", "task", "slot")
    @classmethod
    def _turn_no_spaces(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("pipeline_id")
    @classmethod
    def _optional_turn_no_spaces(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_no_spaces(value, "pipeline_id")

    @field_validator("created_at")
    @classmethod
    def _created_at_valid(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("created_at must be >= 0.0")
        return value

    @field_validator("expires_at")
    @classmethod
    def _expires_at_valid(cls, value: float | None) -> float | None:
        if value is not None and value < 0.0:
            raise ValueError("expires_at must be >= 0.0 when provided")
        return value

    @model_validator(mode="after")
    def _set_default_expiry(self) -> TurnRecord:
        if self.expires_at is None:
            self.expires_at = self.created_at + 86400
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must be >= created_at")
        return self


class NCPResponse(NCPModel):
    """Normalized provider response metadata returned by NCP."""

    content: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cost_usd: float
    model: str
    pipeline_id: str | None = None
    turn_id: str
    latency_ms: int

    @field_validator("model", "turn_id")
    @classmethod
    def _response_no_spaces(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_no_spaces(value, field_name)

    @field_validator("pipeline_id")
    @classmethod
    def _optional_response_no_spaces(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_no_spaces(value, "pipeline_id")

    @field_validator("input_tokens", "output_tokens", "cache_read_tokens", "latency_ms")
    @classmethod
    def _response_non_negative_ints(cls, value: int, info: object) -> int:
        field_name = getattr(info, "field_name", "field")
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0")
        return value

    @field_validator("cost_usd")
    @classmethod
    def _cost_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("cost_usd must be >= 0.0")
        return value


@dataclass
class ConsolidationReport:
    """Result of a consolidation pass over the store."""

    clusters_scanned: int = 0
    merged: int = 0
    tombstoned: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    dry_run: bool = False
    pipeline_id: str | None = None
    merge_log: list[dict] = field(default_factory=list)
