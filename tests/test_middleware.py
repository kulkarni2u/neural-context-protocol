from ncp.middleware.base import Middleware, MiddlewarePipeline
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


class _CaptureMiddleware(Middleware):
    def __init__(self) -> None:
        self.events: list[str] = []

    def pre_assemble(
        self,
        conscious: ConsciousBlock,
        budget: BudgetContext,
    ) -> tuple[ConsciousBlock, BudgetContext] | None:
        self.events.append("pre_assemble")
        return None

    def post_assemble(self, context: str) -> str | None:
        self.events.append("post_assemble")
        return context.upper()

    def pre_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk | None:
        self.events.append("pre_write")
        return chunk

    def post_call(self, response: str, conscious: ConsciousBlock) -> str | None:
        self.events.append("post_call")
        return None


def _make_conscious(**overrides: object) -> ConsciousBlock:
    return ConsciousBlock(
        agent_id=overrides.get("agent_id", "executor"),
        role=overrides.get("role", "build"),
        owns=overrides.get("owns", ["implementation"]),
        must_not=overrides.get("must_not", ["planning"]),
        task=overrides.get("task", "test"),
        slot=overrides.get("slot", "middleware"),
        intent=overrides.get("intent", "verify"),
        pipeline_id=overrides.get("pipeline_id", None),
    )


def test_middleware_pipeline_invokes_all_hooks_in_order() -> None:
    mw = _CaptureMiddleware()
    pipeline = MiddlewarePipeline([mw])

    conscious = _make_conscious()
    budget = BudgetContext()
    chunk = SubconsciousChunk(layer="episodic", content="test", src="synthesis")

    pipeline.pre_assemble(conscious, budget)
    result = pipeline.post_assemble("original")
    pipeline.pre_write(chunk)
    pipeline.post_call("response", conscious)

    assert result == "ORIGINAL"
    assert mw.events == ["pre_assemble", "post_assemble", "pre_write", "post_call"]


def test_middleware_pipeline_reverse_order_for_post_hooks() -> None:
    events: list[str] = []

    class MwA(Middleware):
        def post_assemble(self, context: str) -> str | None:
            events.append("A")
            return context

    class MwB(Middleware):
        def post_assemble(self, context: str) -> str | None:
            events.append("B")
            return context

    pipeline = MiddlewarePipeline([MwA(), MwB()])
    pipeline.post_assemble("ctx")

    assert events == ["B", "A"]


def test_middleware_pipeline_empty_by_default() -> None:
    pipeline = MiddlewarePipeline()
    conscious = _make_conscious()
    budget = BudgetContext()

    c, b = pipeline.pre_assemble(conscious, budget)
    assert c is conscious
    assert b is budget
    assert pipeline.post_assemble("ctx") == "ctx"
