"""Task-ledger service shared by the ``ncp_task`` tool and ``ncp.api``.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 5.

The service owns the action logic (create/claim/heartbeat/release/complete/
cancel/get/list), lazy lease expiry with its doorbell, and the whisper
``task_id`` owner check. The MCP tool layer and the library API are thin
callers, so both paths enforce the same rules. Every whisper emit goes through
:func:`ncp.notify.emit_and_notify`.

These functions call the store's synchronous task methods, so they need a
synchronous store. ``AsyncPgvectorStore`` implements only the ``async_*`` task
methods; its sync forms intentionally raise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ncp.notify import Notifier, emit_and_notify, notify_task
from ncp.stores.base import BaseStore, NCPStoreError
from ncp.types import (
    ProgressPayload,
    ResultPayload,
    TaskRecord,
    Whisper,
    resolve_whisper_thread,
)

if TYPE_CHECKING:
    from ncp.config import NCPConfig

TASK_STATUSES = ("open", "claimed", "done", "failed", "cancelled")


@dataclass(frozen=True)
class TaskSettings:
    """``[tasks]`` and whisper-TTL settings the service needs."""

    enabled: bool = False
    default_lease_seconds: float = 300.0
    max_lease_seconds: float = 3600.0
    default_max_attempts: int = 3
    whisper_ttl: int = 1800

    @classmethod
    def from_config(cls, config: NCPConfig | None) -> TaskSettings:
        if config is None:
            return cls()
        return cls(
            enabled=config.tasks_enabled,
            default_lease_seconds=config.tasks_default_lease_seconds,
            max_lease_seconds=config.tasks_max_lease_seconds,
            default_max_attempts=config.tasks_default_max_attempts,
            whisper_ttl=config.whisper_ttl_default,
        )


def check_task_owner(store: BaseStore, whisper: Whisper, *, enabled: bool = True) -> None:
    """Reject a task-linked progress/result unless it comes from the claiming owner.

    Raises ``ValueError``. ``enabled`` is the ``[tasks].enabled`` flag: when it
    is false such whispers cannot carry a ``task_id`` at all.
    """

    if not enabled:
        raise ValueError(
            f"{whisper.whisper_type} whispers cannot carry task_id: the task ledger is "
            "disabled ([tasks].enabled = false)"
        )
    try:
        task = store.get_task(str(whisper.task_id))
    except NCPStoreError as exc:
        raise ValueError(f"{whisper.whisper_type} whispers cannot carry task_id: {exc}") from exc
    if task is None:
        raise ValueError(f"unknown task_id {whisper.task_id!r}")
    if task.pipeline_id != whisper.pipeline_id:
        raise ValueError(f"task_id {task.task_id!r} belongs to a different pipeline")
    if task.status != "claimed" or task.owner != whisper.from_agent:
        raise ValueError(
            f"{whisper.whisper_type} rejected: only the current owner of a claimed task "
            f"may report on task_id {task.task_id!r}"
        )


class TaskService:
    """Task-ledger actions over a synchronous store, notifier and settings."""

    def __init__(
        self,
        store: BaseStore,
        notifier: Notifier | None,
        settings: TaskSettings,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.settings = settings
        self._log = log

    # -- helpers -------------------------------------------------------

    def require_enabled(self, label: str = "ncp_task") -> None:
        if not self.settings.enabled:
            raise ValueError(f"{label} is disabled: set [tasks].enabled = true")

    def check_task_owner(self, whisper: Whisper) -> None:
        check_task_owner(self.store, whisper, enabled=self.settings.enabled)

    def lease(self, raw: object) -> float:
        default = self.settings.default_lease_seconds
        try:
            lease = default if raw is None else float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            lease = default
        if lease != lease:  # NaN
            lease = default
        return min(max(lease, 1.0), self.settings.max_lease_seconds)

    def expire_and_notify(self, pipeline_id: str | None = None) -> None:
        """Lazy lease expiry; a task that fails by expiry rings its creator."""

        for expired in self.store.expire_task_leases(pipeline_id):
            if expired.status == "failed":
                notify_task(self.notifier, expired, target=expired.created_by)

    def _task_whisper(
        self,
        task: TaskRecord,
        *,
        from_agent: str,
        whisper_type: str,
        payload: object,
        confidence: float = 1.0,
    ) -> Whisper:
        whisper = Whisper(
            from_agent=from_agent,
            target=task.created_by,
            whisper_type=whisper_type,  # type: ignore[arg-type]
            payload=payload,
            confidence=confidence,
            pipeline_id=task.pipeline_id,
            ttl_seconds=self.settings.whisper_ttl,
            in_reply_to=task.origin_whisper_id,
            task_id=task.task_id,
        )
        return resolve_whisper_thread(
            whisper,
            None if whisper.in_reply_to is None else self.store.get_whisper(whisper.in_reply_to),
        )

    @staticmethod
    def _lost(result: object) -> dict[str, object]:
        reason = getattr(result, "reason", None) or "lease_lost"
        return {"ok": False, "reason": reason}

    # -- actions -------------------------------------------------------

    def create(
        self,
        *,
        agent_id: str,
        pipeline_id: str,
        slot: str,
        title: str,
        max_attempts: int | None = None,
        dedupe_key: str | None = None,
        parent_task_id: str | None = None,
        assign_to: str | None = None,
    ) -> dict[str, object]:
        request = None
        task = TaskRecord(
            pipeline_id=pipeline_id,
            slot=slot,
            title=title,
            created_by=agent_id,
            max_attempts=int(max_attempts or self.settings.default_max_attempts),
            dedupe_key=dedupe_key,
            parent_task_id=parent_task_id,
        )
        if assign_to is not None:
            request = Whisper(
                from_agent=agent_id,
                target=assign_to,
                whisper_type="request",
                payload={"ask": task.title},
                confidence=1.0,
                pipeline_id=pipeline_id,
                ttl_seconds=self.settings.whisper_ttl,
                task_id=task.task_id,
            )
            task = task.model_copy(update={"origin_whisper_id": request.whisper_id})
        stored, created = self.store.create_task(task)
        response: dict[str, object] = {"created": created, "task": stored.public_dict()}
        if created and request is not None:
            try:
                emit_and_notify(self.store, self.notifier, resolve_whisper_thread(request))
                response["assigned"] = True
                response["whisper_id"] = request.whisper_id
            except Exception as exc:  # the task exists; report instead of failing a retry
                if self._log is not None:
                    self._log(f"ncp_task create: assign_to request failed: {exc}")
                response["assigned"] = False
        return response

    def claim(
        self,
        *,
        agent_id: str,
        task_id: str | None = None,
        pipeline_id: str | None = None,
        slot: str | None = None,
        lease_seconds: object = None,
    ) -> dict[str, object]:
        if task_id is None and pipeline_id is None:
            raise ValueError("ncp_task claim requires task_id or pipeline_id")
        self.expire_and_notify(pipeline_id)
        claimed = self.store.claim_task(
            agent_id=agent_id,
            task_id=task_id,
            pipeline_id=pipeline_id,
            slot=slot,
            lease_seconds=self.lease(lease_seconds),
        )
        if claimed is None:
            return {"claimed": False}
        return {"claimed": True, "task": claimed.public_dict(), "lease_token": claimed.lease_token}

    def heartbeat(
        self,
        *,
        task_id: str,
        lease_token: str,
        lease_seconds: object = None,
        checkpoint_chunk_id: str | None = None,
        progress: object = None,
        pipeline_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, object]:
        progress_payload = None
        if progress is not None:
            if not isinstance(progress, dict):
                raise ValueError("progress must be an object")
            progress_payload = ProgressPayload.model_validate(progress)
        self.expire_and_notify(pipeline_id)
        result = self.store.heartbeat_task(
            task_id,
            lease_token,
            lease_seconds=self.lease(lease_seconds),
            checkpoint_chunk_id=checkpoint_chunk_id,
        )
        if not result.ok or result.task is None:
            return self._lost(result)
        response: dict[str, object] = {"ok": True, "task": result.task.public_dict()}
        if progress_payload is not None:
            whisper = self._task_whisper(
                result.task,
                from_agent=result.task.owner or agent_id or "unknown",
                whisper_type="progress",
                payload=progress_payload.model_dump(),
                confidence=0.9,
            )
            emit_and_notify(self.store, self.notifier, whisper)
            response["whisper_id"] = whisper.whisper_id
        return response

    def release(
        self,
        *,
        task_id: str,
        lease_token: str,
        error: str | None = None,
        pipeline_id: str | None = None,
    ) -> dict[str, object]:
        self.expire_and_notify(pipeline_id)
        result = self.store.release_task(task_id, lease_token, error=error)
        if not result.ok or result.task is None:
            return self._lost(result)
        if result.task.status == "failed":
            # Attempts exhausted: the release became a failure the creator must hear about.
            notify_task(self.notifier, result.task, target=result.task.created_by)
        return {"ok": True, "task": result.task.public_dict()}

    def complete(
        self,
        *,
        task_id: str,
        lease_token: str,
        summary: str,
        status: str = "done",
        result_chunk_ids: object = None,
        turn_id: str | None = None,
        pipeline_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, object]:
        """Finish a task and emit the linked ``result`` whisper (never records an outcome)."""

        if status not in ("done", "failed"):
            raise ValueError("complete status must be 'done' or 'failed'")
        raw_chunks = result_chunk_ids or []
        if not isinstance(raw_chunks, list):
            raise ValueError("result_chunk_ids must be a list")
        # Validate the result payload before mutating, so a bad summary
        # cannot leave a completed task with no result whisper.
        result_payload = ResultPayload(
            status="succeeded" if status == "done" else "failed",
            summary=summary,
            result_chunk_ids=[str(c) for c in raw_chunks],
            turn_id=turn_id,
        )
        self.expire_and_notify(pipeline_id)
        result = self.store.complete_task(
            task_id,
            lease_token,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            result_chunk_ids=result_payload.result_chunk_ids,
            turn_id=turn_id,
        )
        if not result.ok or result.task is None:
            return self._lost(result)
        whisper = self._task_whisper(
            result.task,
            from_agent=result.task.owner or agent_id or "unknown",
            whisper_type="result",
            payload=result_payload.model_dump(),
        )
        emit_and_notify(self.store, self.notifier, whisper)
        return {
            "ok": True,
            "task": result.task.public_dict(),
            "whisper_id": whisper.whisper_id,
            "result_turn_id": turn_id,
        }

    def cancel(
        self,
        *,
        agent_id: str,
        task_id: str,
        reason: str | None = None,
        pipeline_id: str | None = None,
    ) -> dict[str, object]:
        self.expire_and_notify(pipeline_id)
        result = self.store.cancel_task(task_id, agent_id, reason=reason)
        if not result.ok or result.task is None:
            return self._lost(result)
        if result.previous_status == "claimed" and result.previous_owner:
            notify_task(self.notifier, result.task, target=result.previous_owner, from_agent=agent_id)
        return {"ok": True, "task": result.task.public_dict()}

    def get(self, *, task_id: str, pipeline_id: str | None = None) -> dict[str, object]:
        self.expire_and_notify(pipeline_id)
        found = self.store.get_task(task_id)
        if found is None:
            return {"found": False, "task": None}
        return {"found": True, "task": found.public_dict()}

    def list(
        self,
        *,
        pipeline_id: str,
        status: str | None = None,
        slot: str | None = None,
        owner: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"invalid task status filter {status!r}")
        limit = min(50, max(1, limit))
        self.expire_and_notify(pipeline_id)
        tasks = self.store.list_tasks(
            pipeline_id=pipeline_id,
            status=status,  # type: ignore[arg-type]
            slot=slot,
            owner=owner,
            limit=limit,
        )
        return {"tasks": [t.public_dict() for t in tasks], "count": len(tasks)}


__all__ = ["TASK_STATUSES", "TaskService", "TaskSettings", "check_task_owner"]
