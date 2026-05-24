"""Structured-logging middleware for the assembly pipeline.

Emits structlog events at key assembly lifecycle points.
"""

from __future__ import annotations

import structlog

from ncp.middleware.base import Middleware
from ncp.types import BudgetContext, ConsciousBlock

_logger = structlog.get_logger(__name__)


class LoggingMiddleware(Middleware):
    """Logs assembly lifecycle events via structlog.

    Modes:
        pretty — human-friendly console output (default when attached to a TTY)
        json   — structured JSON lines for log aggregation
    """

    def __init__(self, mode: str = "pretty") -> None:
        structlog.configure(
            processors=(
                [
                    structlog.dev.ConsoleRenderer()
                ]
                if mode == "pretty"
                else [structlog.processors.JSONRenderer()]
            ),
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        self._mode = mode

    def pre_assemble(
        self,
        conscious: ConsciousBlock,
        budget: BudgetContext,
    ) -> tuple[ConsciousBlock, BudgetContext] | None:
        _logger.info(
            "ncp.assembly.start",
            agent_id=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            task=conscious.task,
            slot=conscious.slot,
            pressure=budget.pressure,
        )
        return None

    def post_assemble(self, context: str) -> str | None:
        line_count = len(context.splitlines())
        _logger.info("ncp.assembly.done", lines=line_count, bytes=len(context))
        return None

    def post_call(self, response: str, conscious: ConsciousBlock) -> str | None:
        _logger.info(
            "ncp.call.done",
            agent_id=conscious.agent_id,
            response_bytes=len(response),
            response_lines=len(response.splitlines()),
        )
        return None
