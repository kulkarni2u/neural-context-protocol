"""Budget accuracy (token-efficiency plan item 7)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_budget_tokens, estimate_tokens, reset_encoder_cache
from ncp.types import (
    CONSCIOUS_ITEM_MAX_CHARS,
    CONSCIOUS_LIST_CAPS,
    BudgetContext,
    ConsciousBlock,
    SubconsciousChunk,
)

PIPELINE = "p"


def _conscious(**extra: object) -> ConsciousBlock:
    fields: dict[str, object] = {
        "agent_id": "executor", "role": "build", "owns": ["impl"], "must_not": ["deploy"],
        "task": "payment_retry", "slot": "budget", "intent": "fit", "pipeline_id": PIPELINE,
    }
    fields.update(extra)
    return ConsciousBlock(**fields)  # type: ignore[arg-type]


def _mixed_store(path: Path, seed: int = 0) -> SQLiteStore:
    rng = random.Random(seed)
    store = SQLiteStore(path)
    words = "payment retry backoff ledger idempotent gateway timeout webhook settlement refund".split()
    for i in range(18):
        kind = i % 3
        if kind == 0:
            content = "payment retry " + " ".join(rng.choice(words) for _ in range(rng.randint(15, 90)))
        elif kind == 1:
            content = "payment retry\n" + "\n".join(
                f"    if (x_{j} := retry[{j}]) != {rng.randint(0, 999)}: return {{'k': [{j}, {j * 2}]}}"
                for j in range(rng.randint(4, 14))
            )
        else:
            content = "payment retry " + json.dumps(
                {f"key_{j}": [rng.randint(0, 9999), {"id": f"id-{j}", "ok": bool(j % 2)}] for j in range(rng.randint(3, 16))}
            )
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", content=content, src="tool_result",
            pipeline_id=PIPELINE, written_by=f"w{i % 4}",
        ))
    return store


def test_budget_estimate_is_conservative_and_denser_for_code() -> None:
    prose = "payment retry budget " * 40
    code = '{"a":[1,2,3],"b":{"c":"d"}}' * 30
    assert estimate_budget_tokens(prose) > estimate_tokens(prose)
    assert estimate_budget_tokens(prose, margin=1.0) == estimate_tokens(prose) + (1 if len(prose.strip()) % 4 else 0)
    assert estimate_budget_tokens(code) > estimate_budget_tokens("x" * len(code))
    assert estimate_budget_tokens("   ") == 0


@pytest.mark.parametrize("max_tokens", [250, 400, 700])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_block_never_exceeds_budget_under_conservative_count(tmp_path: Path, max_tokens: int, seed: int) -> None:
    assembler = Assembler(store=_mixed_store(tmp_path / "ncp.db", seed))
    result = assembler.assemble(
        conscious=_conscious(), budget=BudgetContext(), query_text="payment retry", max_tokens=max_tokens, k=8,
    )
    assert estimate_budget_tokens(result.context) <= max_tokens


@pytest.mark.parametrize("max_tokens", [300, 500, 900])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_block_never_exceeds_budget_under_tiktoken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_tokens: int, seed: int
) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    try:
        encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        pytest.skip("cl100k_base encoding data is not available offline")
    monkeypatch.setenv("NCP_TOKEN_UNIT", "tiktoken")
    reset_encoder_cache()
    try:
        assembler = Assembler(store=_mixed_store(tmp_path / "ncp.db", seed))
        result = assembler.assemble(
            conscious=_conscious(), budget=BudgetContext(), query_text="payment retry",
            max_tokens=max_tokens, k=8,
        )
        assert len(encoder.encode(result.context)) <= max_tokens
    finally:
        monkeypatch.delenv("NCP_TOKEN_UNIT")
        reset_encoder_cache()


def test_conscious_lists_are_capped_keeping_most_recent() -> None:
    block = _conscious(
        owns=[f"o{i}" for i in range(40)],
        recent=[f"r{i}" for i in range(30)],
        tried=[f"t{i}" for i in range(30)],
        failed=["f" * 500, *[f"f{i}" for i in range(30)]],
    )
    capped = block.capped()
    assert len(capped.owns) == CONSCIOUS_LIST_CAPS["owns"] and capped.owns[0] == "o0"
    assert capped.tried[-1] == "t29" and len(capped.tried) == CONSCIOUS_LIST_CAPS["tried"]
    assert capped.recent[-1] == "r29"
    assert capped.failed[-1] == "f29"
    assert _conscious(failed=["f" * 500]).capped().failed == ["f" * CONSCIOUS_ITEM_MAX_CHARS]
    assert block.tried[0] == "t0"  # original untouched


def test_assembly_caps_conscious_lists(tmp_path: Path) -> None:
    assembler = Assembler(store=_mixed_store(tmp_path / "ncp.db"))
    result = assembler.assemble(
        conscious=_conscious(tried=[f"attempt_{i}" for i in range(50)]),
        budget=BudgetContext(), query_text="payment retry",
    )
    assert "attempt_49" in result.context and "attempt_0," not in result.context
    assert len(result.conscious.tried) == CONSCIOUS_LIST_CAPS["tried"]


def test_over_budget_trims_optional_lists_and_flags(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store)
    conscious = _conscious(
        recent=["r" * 150] * 8, tried=["t" * 150] * 8, failed=["f" * 150] * 8,
    )
    fixed = estimate_budget_tokens(
        assembler.encoder.assemble(conscious=_conscious(), chunks=[], whispers=[], budget=BudgetContext())
    )
    result = assembler.assemble(
        conscious=conscious, budget=BudgetContext(), query_text="x", max_tokens=fixed + 20,
    )
    assert result.over_budget is True
    assert estimate_budget_tokens(result.context) <= fixed + 20
    assert "tried:" not in result.context and "failed:" not in result.context

    roomy = assembler.assemble(conscious=_conscious(), budget=BudgetContext(), query_text="x", max_tokens=2000)
    assert roomy.over_budget is False


def test_estimate_margin_config_and_removed_dead_key(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(cwd=project)
    assert config.estimate_margin == pytest.approx(0.9)
    assert "max_chunk_tokens" not in config.values["chunking"]
