from __future__ import annotations

from pathlib import Path

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.distill import distill_chunk
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


def _make_conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="executor",
        role="build",
        owns=["implementation"],
        must_not=[],
        task="payment_timeout",
        slot="assemble_context",
        intent="find_answer",
        pipeline_id="pipe_distill",
    )


def _config(tmp_path: Path, *, enabled: bool):
    project = tmp_path / ("enabled" if enabled else "disabled")
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        f"[distillation]\nenabled = {str(enabled).lower()}\nmin_chunk_tokens = 1\n"
    )
    return load_config(cwd=project)


def test_distill_chunk_selects_query_relevant_sentences_in_original_order() -> None:
    content = (
        "Intro boilerplate without the answer. "
        "The retry timeout is 45 seconds for payment callbacks. "
        "More unrelated framing. "
        "Payment callbacks also require idempotency keys."
    )

    distilled = distill_chunk(
        content,
        "payment timeout callbacks",
        max_tokens=40,
    )

    assert distilled == (
        "The retry timeout is 45 seconds for payment callbacks. "
        "Payment callbacks also require idempotency keys."
    )


def test_distillation_keeps_relevant_sentence_under_tight_budget(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    original_content = (
        " ".join(f"boilerplate_{index}" for index in range(80))
        + ". The retry timeout is 45 seconds for payment callbacks. "
        + " ".join(f"appendix_{index}" for index in range(40))
    )
    chunk = SubconsciousChunk(
        chunk_id="sub_oversized",
        layer="semantic",
        content=original_content,
        src="tool_result",
        pipeline_id="pipe_distill",
        relevance=0.99,
    )
    store.write(chunk)
    store.query = lambda *args, **kwargs: [chunk]  # type: ignore[method-assign]

    enabled = Assembler(store=store, config=_config(tmp_path, enabled=True)).assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
        query_text="payment timeout callbacks",
        max_tokens=180,
        k=1,
    )
    disabled = Assembler(store=store, config=_config(tmp_path, enabled=False)).assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
        query_text="payment timeout callbacks",
        max_tokens=180,
        k=1,
    )

    assert estimate_tokens(enabled.context) <= 180
    assert "The retry timeout is 45 seconds for payment callbacks." in enabled.context
    assert "distilled:1" in enabled.context
    assert enabled.chunks[0].distilled is True
    assert disabled.chunks == []

    stored = store.get_chunks_by_ids(["sub_oversized"])[0]
    assert stored.content == original_content
    assert stored.distilled is False
