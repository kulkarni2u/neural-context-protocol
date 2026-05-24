"""Middleware abstraction for the assembly pipeline.

Follows the NCP spec §6.2 hook points:

    pre_assemble(conscious, budget)   → (conscious, budget)
    post_assemble(ncp_context: str)   → str
    pre_write(chunk)                  → chunk
    post_call(response, conscious)    → str

Hooks are called in registration order for pre_*, reverse order for post_*.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence

from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


class Middleware(ABC):
    """Base class for assembly pipeline middleware.

    Subclass and override any combination of hooks.  Each hook is a no-op
    by default (returns its input unchanged).
    """

    def pre_assemble(
        self,
        conscious: ConsciousBlock,
        budget: BudgetContext,
    ) -> tuple[ConsciousBlock, BudgetContext] | None:
        return None

    def post_assemble(self, context: str) -> str | None:
        return None

    def pre_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk | None:
        return None

    def post_call(self, response: str, conscious: ConsciousBlock) -> str | None:
        return None


class MiddlewarePipeline:
    """Chains multiple middleware instances.  Registers with ``add()``."""

    def __init__(self, middleware: Sequence[Middleware] | None = None) -> None:
        self._middleware: list[Middleware] = list(middleware) if middleware else []

    def add(self, mw: Middleware) -> None:
        self._middleware.append(mw)

    @property
    def middleware(self) -> list[Middleware]:
        return list(self._middleware)

    def pre_assemble(
        self,
        conscious: ConsciousBlock,
        budget: BudgetContext,
    ) -> tuple[ConsciousBlock, BudgetContext]:
        for mw in self._middleware:
            result = mw.pre_assemble(conscious, budget)
            if result is not None:
                conscious, budget = result
        return conscious, budget

    def post_assemble(self, context: str) -> str:
        for mw in reversed(self._middleware):
            result = mw.post_assemble(context)
            if result is not None:
                context = result
        return context

    def pre_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        for mw in self._middleware:
            result = mw.pre_write(chunk)
            if result is not None:
                chunk = result
        return chunk

    def post_call(self, response: str, conscious: ConsciousBlock) -> str:
        for mw in reversed(self._middleware):
            result = mw.post_call(response, conscious)
            if result is not None:
                response = result
        return response
