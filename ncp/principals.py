"""Opt-in authenticated principals for the NCP HTTP server.

Without principals the server authenticates with (at most) one shared bearer
token and every tool trusts the agent identity the caller declares. A
``[principals.<name>]`` table in ``.ncp/config.toml`` maps a bearer token to a
principal: an ``agent_id``, an optional pipeline allowlist and a set of
capabilities. When any principal is configured, the HTTP transport resolves
the principal once per request and :class:`ToolAuthorizer` checks every tool
call against it before the handler runs.

This is bearer-token identity: it binds a connection to a principal, it does
not sign individual messages, and it cannot stop a principal from writing
false content under its own name.

Adding a tool: if the tool takes an argument that names the acting agent,
add it to :data:`IDENTITY_ARGS`; if it takes ``pipeline_id``, add it to
:data:`PIPELINE_SCOPED_TOOLS` (the test suite checks both tables against the
tool catalog).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import os
import re
from typing import Any

CAP_ADMIN = "admin"
CAP_VERIFY = "verify"
CAP_RECORD_OUTCOME = "record_outcome"
CAPABILITIES = frozenset({CAP_ADMIN, CAP_VERIFY, CAP_RECORD_OUTCOME})

# Name of the implicit principal the legacy [server].auth_token maps to when
# principals are also configured.
LEGACY_PRINCIPAL_NAME = "legacy_auth_token"

_PRINCIPAL_KEYS = frozenset({"agent_id", "token_sha256", "token_env", "pipelines", "capabilities"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# Tool -> top-level arguments that name the acting agent. Each one must equal
# the principal's agent_id (or is filled with it when omitted, if "fill").
# Nested identity arguments (ncp_post_turn memory_chunks[].written_by) are
# handled in ToolAuthorizer._check_post_turn.
IDENTITY_ARGS: dict[str, tuple[tuple[str, bool], ...]] = {
    "ncp_get_context": (("agent_id", True),),
    "ncp_post_turn": (("agent_id", True),),
    "ncp_write_memory": (("written_by", True), ("owner", False)),
    "ncp_emit_whisper": (("from", True),),
    "ncp_wait_whispers": (("agent_id", True),),
    "ncp_task": (("agent_id", True),),
    "ncp_overview": (("agent_id", True),),
    "ncp_fetch": (("agent_id", True),),
    "ncp_record_decision": (("agent_id", True),),
    "ncp_compile_decision_query": (("agent_id", True),),
    "ncp_remember": (("written_by", True),),
}

# Tools whose pipeline scope is the top-level ``pipeline_id`` argument. A
# principal with a pipeline allowlist must name an allowed pipeline_id.
PIPELINE_SCOPED_TOOLS = frozenset({
    "ncp_get_context",
    "ncp_post_turn",
    "ncp_write_memory",
    "ncp_emit_whisper",
    "ncp_wait_whispers",
    "ncp_task",
    "ncp_fetch",
    "ncp_record_decision",
    "ncp_compile_decision_query",
    "ncp_lookup_memo",
    "ncp_record_memo",
    "ncp_verify_memo",
    "ncp_remember",
    "ncp_recall",
    "ncp_improve",
    "ncp_ontology_query",
    "ncp_ontology_explain",
    "ncp_ontology_assert",
    "ncp_ontology_revoke",
})

# Ontology tools whose ``pipeline_id`` may also be the reserved ``global``
# namespace, which every principal may read. Writes to it are gated by the
# ontology auth mapping (admin only), not here.
ONTOLOGY_NAMESPACE_TOOLS = frozenset({
    "ncp_ontology_query",
    "ncp_ontology_explain",
    "ncp_ontology_assert",
    "ncp_ontology_revoke",
})
ONTOLOGY_GLOBAL_NAMESPACE = "global"

# Tools with no acting-agent argument and no pipeline_id, scoped by lookups
# instead (documented so the catalog check can tell "reviewed" from "missed").
LOOKUP_SCOPED_TOOLS = frozenset({
    "ncp_overview", "ncp_get_decision", "ncp_record_outcome",
    # Static server metadata: no pipeline data, nothing to scope.
    "ncp_ontology_capabilities",
})

_TASK_ID_ACTIONS = frozenset({"heartbeat", "release", "complete", "cancel", "get"})
_MEMO_TOOLS = frozenset({"ncp_lookup_memo", "ncp_record_memo", "ncp_verify_memo"})


class PrincipalConfigError(ValueError):
    """The ``[principals]`` config is invalid; the server must not start."""


class AuthorizationError(Exception):
    """A tool call or read the principal is not allowed to make."""


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    agent_id: str | None
    pipelines: frozenset[str] | None
    capabilities: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return CAP_ADMIN in self.capabilities

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def pipeline_allowed(self, pipeline_id: object) -> bool:
        if self.pipelines is None:
            return True
        return pipeline_id is not None and str(pipeline_id) in self.pipelines

    @property
    def fetch_session_key(self) -> str:
        return f"principal:{self.name}"


def hash_token(token: str) -> str:
    """Hex SHA-256 of a bearer token, the form stored as ``token_sha256``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _string_list(name: str, key: str, raw: object) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise PrincipalConfigError(f"[principals.{name}].{key} must be an array of non-empty strings")
    return [item.strip() for item in raw]


def _parse_one(name: str, raw: object, env: Mapping[str, str]) -> tuple[Principal, str]:
    if not isinstance(raw, dict):
        raise PrincipalConfigError(f"[principals.{name}] must be a table")
    if "token" in raw:
        raise PrincipalConfigError(
            f"[principals.{name}] has a plaintext 'token'; store token_sha256 "
            "(hex SHA-256 of the token) or token_env (an environment variable name) instead"
        )
    unknown = sorted(set(raw) - _PRINCIPAL_KEYS)
    if unknown:
        raise PrincipalConfigError(f"[principals.{name}] has unknown keys {unknown}")

    agent_id = raw.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip() or any(c.isspace() for c in agent_id.strip()):
        raise PrincipalConfigError(f"[principals.{name}].agent_id must be a non-empty string without whitespace")

    has_hash = "token_sha256" in raw
    has_env = "token_env" in raw
    if has_hash == has_env:
        raise PrincipalConfigError(f"[principals.{name}] needs exactly one of token_sha256 or token_env")
    if has_hash:
        digest = raw["token_sha256"]
        if not isinstance(digest, str) or not _SHA256_HEX.match(digest.strip().lower()):
            raise PrincipalConfigError(f"[principals.{name}].token_sha256 must be 64 hex characters")
        digest = digest.strip().lower()
    else:
        var = raw["token_env"]
        if not isinstance(var, str) or not var.strip():
            raise PrincipalConfigError(f"[principals.{name}].token_env must name an environment variable")
        token = env.get(var.strip(), "")
        if not token:
            raise PrincipalConfigError(
                f"[principals.{name}].token_env names {var.strip()!r}, which is unset or empty"
            )
        digest = hash_token(token)

    pipelines: frozenset[str] | None = None
    if "pipelines" in raw:
        listed = _string_list(name, "pipelines", raw["pipelines"])
        if not listed:
            raise PrincipalConfigError(
                f"[principals.{name}].pipelines is empty; omit it to allow all pipelines"
            )
        pipelines = frozenset(listed)

    capabilities = frozenset(_string_list(name, "capabilities", raw.get("capabilities", [])))
    bad = sorted(capabilities - CAPABILITIES)
    if bad:
        raise PrincipalConfigError(
            f"[principals.{name}].capabilities has unknown values {bad}; expected a subset of {sorted(CAPABILITIES)}"
        )
    return Principal(name=name, agent_id=agent_id.strip(), pipelines=pipelines, capabilities=capabilities), digest


class PrincipalRegistry:
    """Maps presented bearer tokens to principals."""

    def __init__(self, entries: list[tuple[Principal, str]], *, legacy_token: str | None = None) -> None:
        self._entries = entries
        self._legacy_digest = hash_token(legacy_token) if legacy_token else None
        self.legacy_principal = Principal(
            name=LEGACY_PRINCIPAL_NAME,
            agent_id=None,
            pipelines=None,
            capabilities=CAPABILITIES,
        )

    @property
    def principals(self) -> list[Principal]:
        return [principal for principal, _ in self._entries]

    @property
    def has_legacy_token(self) -> bool:
        return self._legacy_digest is not None

    def resolve_token(self, token: str) -> Principal | None:
        """Return the principal for a raw token, or None. Compares every entry."""
        if not token:
            return None
        presented = hash_token(token)
        found: Principal | None = None
        for principal, digest in self._entries:
            if hmac.compare_digest(presented, digest) and found is None:
                found = principal
        if found is None and self._legacy_digest is not None:
            if hmac.compare_digest(presented, self._legacy_digest):
                found = self.legacy_principal
        return found

    def resolve_authorization(self, header: str | None) -> Principal | None:
        """Resolve an ``Authorization: Bearer <token>`` header value."""
        if not header:
            return None
        scheme, _, token = header.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self.resolve_token(token.strip())


def load_principals(
    raw: object,
    *,
    legacy_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> PrincipalRegistry | None:
    """Parse the ``[principals]`` table. ``None`` when no principal is configured."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise PrincipalConfigError("[principals] must be a table of [principals.<name>] tables")
    runtime_env = os.environ if env is None else env
    entries: list[tuple[Principal, str]] = []
    seen: dict[str, str] = {}
    for name in sorted(raw):
        principal, digest = _parse_one(str(name), raw[name], runtime_env)
        if digest in seen:
            raise PrincipalConfigError(
                f"[principals.{name}] and [principals.{seen[digest]}] use the same token"
            )
        if legacy_token and hmac.compare_digest(digest, hash_token(legacy_token)):
            raise PrincipalConfigError(
                f"[principals.{name}] uses the same token as [server].auth_token"
            )
        seen[digest] = str(name)
        entries.append((principal, digest))
    if not entries:
        return None
    return PrincipalRegistry(entries, legacy_token=legacy_token)


# ----------------------------------------------------------------------
# Authorization


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


class ToolAuthorizer:
    """Checks tool calls against a principal. Needs the store for lookups.

    ``check_call`` returns the arguments the handler should run with (a copy:
    omitted identity arguments filled with the principal's agent_id, and the
    fetch-budget session_id replaced) or raises :class:`AuthorizationError`.
    ``filter_result`` hides results that a lookup-scoped tool returned from
    outside the principal's pipelines.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _bind(principal: Principal, args: dict[str, object], key: str, *, fill: bool, where: str) -> None:
        if principal.is_admin or principal.agent_id is None:
            return
        value = _str_or_none(args.get(key))
        if value is None:
            if fill:
                args[key] = principal.agent_id
            return
        if value != principal.agent_id:
            raise AuthorizationError(
                f"{where}: {key}={value!r} does not match the authenticated principal "
                f"{principal.name!r} (agent_id {principal.agent_id!r})"
            )

    @staticmethod
    def _require_pipeline(principal: Principal, pipeline_id: object, *, where: str) -> None:
        if principal.pipelines is None:
            return
        if _str_or_none(pipeline_id) is None:
            raise AuthorizationError(
                f"{where}: principal {principal.name!r} is limited to pipelines "
                f"{sorted(principal.pipelines)}; pipeline_id is required"
            )
        if str(pipeline_id) not in principal.pipelines:
            raise AuthorizationError(
                f"{where}: principal {principal.name!r} may not access pipeline {str(pipeline_id)!r}"
            )

    @staticmethod
    def _require_verify_for_src(principal: Principal, src: object, *, where: str) -> None:
        if str(src) == "user_verified" and not principal.has(CAP_VERIFY):
            raise AuthorizationError(
                f"{where}: src='user_verified' requires the 'verify' capability "
                f"(principal {principal.name!r} does not have it)"
            )

    def _check_chunk_id_not_foreign(self, principal: Principal, chunk_id: object, *, where: str) -> None:
        """Writing with an existing chunk_id overwrites that chunk: only its author may."""
        if principal.is_admin or _str_or_none(chunk_id) is None:
            return
        for existing in self.store.get_chunks_by_ids([str(chunk_id)]):
            if existing.written_by != principal.agent_id:
                raise AuthorizationError(
                    f"{where}: chunk_id {str(chunk_id)!r} already exists and was written by "
                    f"{existing.written_by!r}; only its author may overwrite it"
                )

    # -- entry points ----------------------------------------------------

    def check_call(self, principal: Principal, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        args = dict(arguments)
        for key, fill in IDENTITY_ARGS.get(tool, ()):
            self._bind(principal, args, key, fill=fill, where=tool)
        if tool in PIPELINE_SCOPED_TOOLS:
            self._check_pipeline_scope(principal, tool, args)

        if tool in ("ncp_get_context", "ncp_fetch"):
            # The per-turn ncp_fetch budget is keyed by principal, never by a
            # caller-chosen session_id. Admin acts for many agents, so its
            # budget is per acting agent.
            key = principal.fetch_session_key
            if principal.is_admin:
                key = f"{key}:{_str_or_none(args.get('pipeline_id')) or '-'}:{_str_or_none(args.get('agent_id')) or '-'}"
            args["session_id"] = key
        if tool == "ncp_get_context":
            self._check_recent_refs(principal, args.get("recent"))
        elif tool == "ncp_improve":
            # Consolidation merges near-duplicate chunks across authors: it
            # deletes the losers, marks the keeper as superseding them and
            # aliases their ids to it, which also moves outcome credit.
            if not principal.is_admin:
                raise AuthorizationError(
                    f"ncp_improve requires the 'admin' capability (principal {principal.name!r} does not have it): "
                    "consolidation can delete or supersede other agents' chunks"
                )
        elif tool == "ncp_emit_whisper":
            self._check_dissent_ref(principal, args)
        elif tool == "ncp_write_memory":
            self._require_verify_for_src(principal, args.get("src"), where=tool)
            self._check_chunk_id_not_foreign(principal, args.get("chunk_id"), where=tool)
            self._check_supersedes(principal, args.get("supersedes"))
        elif tool == "ncp_post_turn":
            self._check_post_turn(principal, args)
        elif tool == "ncp_task":
            self._check_task(principal, args)
        elif tool == "ncp_overview":
            self._check_overview(principal, args)
        elif tool == "ncp_record_outcome":
            self._check_record_outcome(principal, args)
        elif tool == "ncp_verify_memo":
            if not principal.has(CAP_VERIFY):
                raise AuthorizationError(
                    f"ncp_verify_memo requires the 'verify' capability (principal {principal.name!r} does not have it)"
                )
        return args

    def filter_result(self, principal: Principal, tool: str, result: object) -> object:
        if tool == "ncp_get_decision" and principal.pipelines is not None and isinstance(result, dict):
            decision = result.get("decision")
            if isinstance(decision, dict) and not principal.pipeline_allowed(decision.get("pipeline_id")):
                return {"found": False, "decision_id": decision.get("decision_id")}
        return result

    # -- per-tool checks -----------------------------------------------

    def _check_pipeline_scope(self, principal: Principal, tool: str, args: dict[str, object]) -> None:
        if principal.pipelines is None:
            return
        if tool == "ncp_task":
            action = str(args.get("action", ""))
            # task_id actions are scoped by the task's own pipeline (below);
            # a pipeline_id passed alongside must still be allowed.
            if action in _TASK_ID_ACTIONS and _str_or_none(args.get("pipeline_id")) is None:
                return
        if tool in ONTOLOGY_NAMESPACE_TOOLS and _str_or_none(args.get("pipeline_id")) == ONTOLOGY_GLOBAL_NAMESPACE:
            return
        if tool in _MEMO_TOOLS and args.get("signature") is not None:
            raise AuthorizationError(
                f"{tool}: principal {principal.name!r} is pipeline-limited; pass task/context/pipeline_id "
                "instead of an explicit signature (a raw signature is not pipeline-scoped)"
            )
        self._require_pipeline(principal, args.get("pipeline_id"), where=tool)

    def _check_supersedes(self, principal: Principal, supersedes: object) -> None:
        if principal.is_admin or _str_or_none(supersedes) is None:
            return
        for old in self.store.get_chunks_by_ids([str(supersedes)]):
            if old.written_by != principal.agent_id:
                raise AuthorizationError(
                    f"ncp_write_memory: supersedes {str(supersedes)!r} was written by {old.written_by!r}; "
                    "a principal may only supersede its own chunks"
                )

    def _lookup_turn(self, turn_id: str) -> Any:
        try:
            return self.store.resolve_recent_ref(f"r:sub/{turn_id}")
        except Exception as exc:  # noqa: BLE001 - fail closed when the lookup cannot run
            raise AuthorizationError(f"cannot verify turn {turn_id!r}: {exc}") from exc

    def _check_recent_refs(self, principal: Principal, recent: object) -> None:
        """A recent ref resolves a turn record by id with no pipeline filter."""
        if principal.pipelines is None or not isinstance(recent, (list, tuple)):
            return
        for ref in recent:
            text = _str_or_none(ref)
            if text is None or not text.startswith("r:sub/"):
                continue
            record = self._lookup_turn(text.split("/", 1)[1])
            if record is not None and not principal.pipeline_allowed(record.pipeline_id):
                raise AuthorizationError(
                    f"ncp_get_context: recent ref {text!r} names a turn outside the pipelines "
                    f"principal {principal.name!r} may access"
                )

    def _check_dissent_ref(self, principal: Principal, args: dict[str, object]) -> None:
        """A dissent whisper raises the dissent count of the chunk named by ``ref``."""
        if principal.pipelines is None or str(args.get("type", "")) != "dissent":
            return
        ref = _str_or_none(args.get("ref"))
        if ref is None:
            return
        for chunk in self.store.get_chunks_by_ids([ref]):
            if not principal.pipeline_allowed(chunk.pipeline_id):
                raise AuthorizationError(
                    f"ncp_emit_whisper: dissent ref {ref!r} is outside the pipelines "
                    f"principal {principal.name!r} may access"
                )

    def _check_turn_ownership(self, principal: Principal, args: dict[str, object]) -> None:
        """Posting with an existing turn_id replaces that turn record and its chunk links."""
        turn_id = _str_or_none(args.get("turn_id"))
        if turn_id is not None:
            record = self._lookup_turn(turn_id)
            if record is not None:
                if not principal.is_admin and record.agent_id != principal.agent_id:
                    raise AuthorizationError(
                        f"ncp_post_turn: turn_id {turn_id!r} already exists and belongs to "
                        f"{record.agent_id!r}; only its author may post it again"
                    )
                if not principal.pipeline_allowed(record.pipeline_id):
                    raise AuthorizationError(
                        f"ncp_post_turn: turn_id {turn_id!r} belongs to a pipeline principal "
                        f"{principal.name!r} may not access"
                    )
        retrieved = args.get("retrieved_chunk_ids")
        if principal.pipelines is None or not isinstance(retrieved, (list, tuple)):
            return
        ids = [text for text in (_str_or_none(item) for item in retrieved) if text is not None]
        for chunk in self.store.get_chunks_by_ids(ids) if ids else ():
            # Global-scope chunks are legitimately retrieved from any pipeline.
            if getattr(chunk, "scope", "pipeline") == "global":
                continue
            if not principal.pipeline_allowed(chunk.pipeline_id):
                raise AuthorizationError(
                    f"ncp_post_turn: retrieved_chunk_ids names {chunk.chunk_id!r}, which is outside "
                    f"the pipelines principal {principal.name!r} may access"
                )

    def _check_post_turn(self, principal: Principal, args: dict[str, object]) -> None:
        self._check_turn_ownership(principal, args)
        raw_items = args.get("memory_chunks")
        if not isinstance(raw_items, list):
            return
        items: list[object] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                items.append(item)  # the handler rejects non-objects
                continue
            bound = dict(item)
            where = f"ncp_post_turn memory_chunks[{index}]"
            # The handler defaults an omitted written_by to agent_id, which is
            # already bound, so only an explicit value needs checking.
            self._bind(principal, bound, "written_by", fill=False, where=where)
            self._require_verify_for_src(principal, bound.get("src"), where=where)
            self._check_chunk_id_not_foreign(principal, bound.get("chunk_id"), where=where)
            items.append(bound)
        args["memory_chunks"] = items

    def _check_task(self, principal: Principal, args: dict[str, object]) -> None:
        if principal.pipelines is None:
            return
        action = str(args.get("action", ""))
        parent_id = _str_or_none(args.get("parent_task_id"))
        if parent_id is not None:
            try:
                parent = self.store.get_task(parent_id)
            except Exception as exc:  # noqa: BLE001 - store without a ledger
                raise AuthorizationError(f"ncp_task: cannot verify parent task {parent_id!r}: {exc}") from exc
            if parent is not None and not principal.pipeline_allowed(parent.pipeline_id):
                raise AuthorizationError(
                    f"ncp_task: parent_task_id {parent_id!r} is outside the pipelines "
                    f"principal {principal.name!r} may access"
                )
        if action not in _TASK_ID_ACTIONS:
            return
        task_id = _str_or_none(args.get("task_id"))
        if task_id is None:
            return  # the handler rejects a missing task_id
        try:
            task = self.store.get_task(task_id)
        except Exception as exc:  # noqa: BLE001 - store without a ledger
            raise AuthorizationError(f"ncp_task: cannot verify the pipeline of task {task_id!r}: {exc}") from exc
        if task is None:
            raise AuthorizationError(f"ncp_task: unknown task_id {task_id!r}")
        if not principal.pipeline_allowed(task.pipeline_id):
            raise AuthorizationError(
                f"ncp_task: principal {principal.name!r} may not access pipeline {task.pipeline_id!r}"
            )

    def _check_overview(self, principal: Principal, args: dict[str, object]) -> None:
        if principal.pipelines is None:
            return
        raw = args.get("pipelines")
        if not isinstance(raw, (list, tuple)) or not raw:
            raise AuthorizationError(
                f"ncp_overview: principal {principal.name!r} is limited to pipelines "
                f"{sorted(principal.pipelines)}; pass 'pipelines' naming only those"
            )
        for pipeline_id in raw:
            self._require_pipeline(principal, pipeline_id, where="ncp_overview")

    def _check_record_outcome(self, principal: Principal, args: dict[str, object]) -> None:
        if not principal.has(CAP_RECORD_OUTCOME):
            raise AuthorizationError(
                f"ncp_record_outcome requires the 'record_outcome' capability "
                f"(principal {principal.name!r} does not have it)"
            )
        raw_ids = args.get("chunk_ids") or []
        chunk_ids = [str(c) for c in raw_ids] if isinstance(raw_ids, list) else []
        restricted = principal.pipelines is not None
        if principal.is_admin and not restricted:
            return
        if not chunk_ids:
            # A turn_id-only outcome is resolved to chunks inside the store,
            # after this check could see them, so neither authorship nor
            # pipeline can be verified here. Name the chunks instead.
            raise AuthorizationError(
                "ncp_record_outcome: this principal must pass explicit chunk_ids "
                "(turn_id-only outcomes cannot be checked for self-crediting or pipeline scope)"
            )
        if chunk_ids:
            found = {chunk.chunk_id: chunk for chunk in self.store.get_chunks_by_ids(chunk_ids)}
            for chunk_id in chunk_ids:
                chunk = found.get(chunk_id)
                if chunk is None:
                    raise AuthorizationError(
                        f"ncp_record_outcome: chunk {chunk_id!r} is not a live chunk; its author cannot be checked"
                    )
                if not principal.is_admin and chunk.written_by == principal.agent_id:
                    raise AuthorizationError(
                        f"ncp_record_outcome: chunk {chunk_id!r} was written by {principal.agent_id!r}; "
                        "a principal may not record outcomes crediting its own chunks"
                    )
                if not principal.pipeline_allowed(chunk.pipeline_id):
                    raise AuthorizationError(
                        f"ncp_record_outcome: chunk {chunk_id!r} is in pipeline {chunk.pipeline_id!r}, "
                        f"outside principal {principal.name!r}'s pipelines"
                    )
        decision_id = _str_or_none(args.get("decision_id"))
        if decision_id is not None:
            decision = self.store.get_decision(decision_id)
            if decision is not None:
                if not principal.pipeline_allowed(decision.pipeline_id):
                    raise AuthorizationError(
                        f"ncp_record_outcome: decision {decision_id!r} is outside principal "
                        f"{principal.name!r}'s pipelines"
                    )
                if not principal.is_admin and decision.agent_id == principal.agent_id:
                    raise AuthorizationError(
                        f"ncp_record_outcome: decision {decision_id!r} was made by {principal.agent_id!r}; "
                        "a principal may not link outcomes to its own decisions"
                    )


# ----------------------------------------------------------------------
# HTTP GET endpoints


def authorize_read(principal: Principal, *, pipeline_id: str | None, endpoint: str) -> None:
    """``/api/*`` reads: pipeline ACL, and ``admin`` for unscoped reads."""
    if pipeline_id is None:
        if principal.pipelines is not None:
            raise AuthorizationError(
                f"{endpoint}: principal {principal.name!r} is limited to pipelines "
                f"{sorted(principal.pipelines)}; pipeline_id is required"
            )
        if not principal.is_admin:
            raise AuthorizationError(f"{endpoint}: an unscoped read (no pipeline_id) requires 'admin'")
        return
    if not principal.pipeline_allowed(pipeline_id):
        raise AuthorizationError(f"{endpoint}: principal {principal.name!r} may not access pipeline {pipeline_id!r}")


def authorize_events(principal: Principal, *, agent_id: str | None, pipeline_id: str | None) -> str | None:
    """``/events``: bind agent_id to the principal and apply the pipeline ACL.

    Returns the agent_id to subscribe as (filled from the principal when the
    query omits it); ``None`` when an admin omits it.
    """
    resolved = agent_id
    if not principal.is_admin and principal.agent_id is not None:
        if agent_id and agent_id != principal.agent_id:
            raise AuthorizationError(
                f"/events: agent_id={agent_id!r} does not match the authenticated principal "
                f"{principal.name!r} (agent_id {principal.agent_id!r})"
            )
        resolved = principal.agent_id
    if principal.pipelines is not None:
        if pipeline_id is None:
            raise AuthorizationError(
                f"/events: principal {principal.name!r} is limited to pipelines "
                f"{sorted(principal.pipelines)}; pipeline_id is required"
            )
        if pipeline_id not in principal.pipelines:
            raise AuthorizationError(f"/events: principal {principal.name!r} may not access pipeline {pipeline_id!r}")
    return resolved or None
