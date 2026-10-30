"""Normalized provider usage, cache billing, streaming usage and whisper acks.

Regressions covered:
- OpenAI ``prompt_tokens`` includes ``cached_tokens``; billing both at the
  input and cache-read price charged cached input twice.
- Anthropic cache-write tokens were parsed but never priced.
- OpenAI streaming never reported usage, so a stream either billed an
  estimate or re-billed the previous call's stale ``last_usage``.
- ncp.run/ncp.stream never acknowledged the whispers they delivered.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ncp
from ncp.adapters.base import BaseAdapter, NCPAdapterError, TokenUsage
from ncp.config import NCPConfig
from ncp.costs import calculate_cost
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper


def _openai_usage(prompt: int, completion: int, cached: int | None = None, reasoning: int | None = None):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=None if cached is None else SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=None if reasoning is None else SimpleNamespace(reasoning_tokens=reasoning),
    )


# ── normalization ───────────────────────────────────────────────────────────


def test_openai_shape_splits_cached_from_uncached_input() -> None:
    usage = BaseAdapter._usage_from_openai_shape(
        SimpleNamespace(usage=_openai_usage(1000, 50, cached=800, reasoning=20))
    )
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens) == (200, 800)
    assert usage.output_tokens == 50 and usage.reasoning_tokens == 20


@pytest.mark.parametrize(
    ("cached", "expected"),
    [(None, (1000, 0)), (0, (1000, 0)), (1000, (0, 1000)), (5000, (0, 1000)), (-3, (1000, 0))],
)
def test_openai_shape_cached_edge_cases(cached: int | None, expected: tuple[int, int]) -> None:
    usage = BaseAdapter._usage_from_openai_shape(SimpleNamespace(usage=_openai_usage(1000, 1, cached=cached)))
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens) == expected


def test_openai_shape_missing_usage_is_none() -> None:
    assert BaseAdapter._usage_from_openai_shape(SimpleNamespace(usage=None)) is None


def test_gemini_usage_splits_cached_and_bills_thoughts_as_output() -> None:
    pytest.importorskip("google.genai")
    from ncp.adapters.gemini import GeminiAdapter

    usage = GeminiAdapter._usage_from_gemini(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=1000,
                cached_content_token_count=600,
                candidates_token_count=40,
                thoughts_token_count=25,
            )
        )
    )
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens) == (400, 600)
    assert (usage.output_tokens, usage.reasoning_tokens) == (65, 25)


# ── OpenAI streaming usage ──────────────────────────────────────────────────


def _openai_adapter():
    pytest.importorskip("openai")
    from ncp.adapters.openai import OpenAIAdapter

    return OpenAIAdapter(api_key="test-key")


def _delta_chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)


def test_openai_stream_requests_and_parses_final_usage_chunk() -> None:
    adapter = _openai_adapter()
    final = SimpleNamespace(choices=[], usage=_openai_usage(900, 7, cached=300))

    with patch.object(adapter._client.chat.completions, "create") as create:
        create.return_value = iter([_delta_chunk("Par"), _delta_chunk("is"), final])
        assert "".join(adapter.stream("ctx", "turn")) == "Paris"

    assert create.call_args.kwargs["stream_options"] == {"include_usage": True}
    assert adapter.last_usage is not None
    assert (adapter.last_usage.input_tokens, adapter.last_usage.cache_read_tokens) == (600, 300)
    assert adapter.last_usage.output_tokens == 7


def test_openai_stream_without_usage_does_not_keep_stale_usage() -> None:
    adapter = _openai_adapter()
    adapter.last_usage = TokenUsage(input_tokens=999, output_tokens=999)

    with patch.object(adapter._client.chat.completions, "create") as create:
        create.return_value = iter([_delta_chunk("hi")])
        list(adapter.stream("ctx", "turn"))

    assert adapter.last_usage is None


def test_openai_interrupted_stream_reports_no_usage() -> None:
    adapter = _openai_adapter()
    adapter.last_usage = TokenUsage(input_tokens=999, output_tokens=999)

    def chunks():
        yield _delta_chunk("par")
        raise RuntimeError("connection reset")

    with patch.object(adapter._client.chat.completions, "create") as create:
        create.return_value = chunks()
        with pytest.raises(RuntimeError):
            list(adapter.stream("ctx", "turn"))

    assert adapter.last_usage is None


def test_openai_failed_call_clears_previous_usage() -> None:
    adapter = _openai_adapter()
    adapter.last_usage = TokenUsage(input_tokens=999, output_tokens=999)

    with patch.object(adapter._client.chat.completions, "create", side_effect=RuntimeError("boom")):
        with pytest.raises(NCPAdapterError):
            adapter.call("ctx", "turn")

    assert adapter.last_usage is None


# ── billing through ncp.run / ncp.stream ────────────────────────────────────


class _UsageAdapter(BaseAdapter):
    """Reports a fixed usage on call(); stream() reports none (like an aborted usage chunk)."""

    def __init__(self, usage: TokenUsage | None, model: str) -> None:
        self._usage = usage
        self._model = model
        self.contexts: list[str] = []

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.contexts.append(ncp_context)
        self.last_usage = self._usage
        return "ok"

    def stream(self, ncp_context: str, user_turn: str):
        self.contexts.append(ncp_context)
        yield "ok"


def _project(tmp_path: Path) -> tuple[SQLiteStore, NCPConfig]:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = ncp.configure(cwd=project)  # default pricing table
    return SQLiteStore(project / ".ncp" / "store.db", config=config), config


def _agent():
    return ncp.agent(
        id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="billing_check",
        slot="usage",
        intent="verify_costs",
    )


def test_run_bills_openai_cached_input_once(tmp_path: Path) -> None:
    store, config = _project(tmp_path)
    usage = BaseAdapter._usage_from_openai_shape(SimpleNamespace(usage=_openai_usage(1000, 100, cached=800)))
    adapter = _UsageAdapter(usage, "gpt-4o")

    response = ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)

    expected = calculate_cost(
        model="gpt-4o", input_tokens=200, output_tokens=100, cache_read_tokens=800, pricing=config.pricing
    ).total_cost_usd
    assert (response.input_tokens, response.cache_read_tokens) == (200, 800)
    assert response.cost_usd == pytest.approx(expected)


def test_run_bills_anthropic_cache_writes(tmp_path: Path) -> None:
    store, config = _project(tmp_path)
    usage = TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=2000)
    adapter = _UsageAdapter(usage, "claude-sonnet-4-20250514")

    response = ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)

    breakdown = calculate_cost(
        model="claude-sonnet-4-20250514",
        input_tokens=100,
        output_tokens=50,
        cache_write_tokens=2000,
        pricing=config.pricing,
    )
    assert breakdown.cache_write_cost_usd > 0
    assert response.cache_write_tokens == 2000
    assert response.cost_usd == pytest.approx(breakdown.total_cost_usd)


def test_stream_never_bills_usage_left_by_a_previous_call(tmp_path: Path) -> None:
    store, config = _project(tmp_path)
    adapter = _UsageAdapter(TokenUsage(input_tokens=10_000, output_tokens=10_000), "gpt-4o")

    first = ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)
    assert first.cost_source == "measured"

    "".join(ncp.stream(agent=_agent(), turn="go again", adapter=adapter, store=store, config=config))
    costs = store.cost_summary(limit=5)
    latest = costs["recent_entries"][0]
    # The stream reported no usage: it is estimated, not re-billed at 10k/10k.
    assert latest["cost_usd"] == 0.0


# ── whisper acknowledgement ─────────────────────────────────────────────────


def _queue_whisper(store: SQLiteStore, whisper_id: str) -> None:
    store.emit_whisper(
        Whisper(
            whisper_id=whisper_id,
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload={"slice": "billing", "files": ["ncp/costs.py"], "ask": "check cache billing"},
            confidence=0.9,
        )
    )


def _pending(store: SQLiteStore) -> list[str]:
    return [w.whisper_id for w in store.peek_whispers(agent_id="executor", pipeline_id=None, max_items=10)]


@pytest.mark.parametrize("mode", ["run", "stream"])
def test_delivered_whispers_are_acknowledged(tmp_path: Path, mode: str) -> None:
    store, config = _project(tmp_path)
    _queue_whisper(store, "wsp_billing_1")
    adapter = _UsageAdapter(None, "gpt-4o")

    if mode == "run":
        response = ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)
        assert response.evidence is not None and response.evidence.whisper_ids == ["wsp_billing_1"]
    else:
        "".join(ncp.stream(agent=_agent(), turn="go", adapter=adapter, store=store, config=config))

    assert "check cache billing" in adapter.contexts[-1]
    assert _pending(store) == []


def test_failed_request_leaves_whispers_pending(tmp_path: Path) -> None:
    store, config = _project(tmp_path)
    _queue_whisper(store, "wsp_billing_2")
    adapter = MagicMock(spec=BaseAdapter)
    adapter.ctx_window = 200000
    adapter.call.side_effect = NCPAdapterError("provider down")

    with pytest.raises(NCPAdapterError):
        ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)

    assert _pending(store) == ["wsp_billing_2"]


def test_abandoned_stream_leaves_whispers_pending(tmp_path: Path) -> None:
    store, config = _project(tmp_path)
    _queue_whisper(store, "wsp_billing_3")

    class _TwoChunk(_UsageAdapter):
        def stream(self, ncp_context: str, user_turn: str):
            yield "a"
            yield "b"

    gen = ncp.stream(agent=_agent(), turn="go", adapter=_TwoChunk(None, "gpt-4o"), store=store, config=config)
    next(gen)
    gen.close()

    assert _pending(store) == ["wsp_billing_3"]


def test_memo_hit_does_not_acknowledge_whispers(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = NCPConfig(values={"memoization": {"enabled": True}}, project_root=project)
    store = SQLiteStore(project / ".ncp" / "store.db", config=config)
    adapter = _UsageAdapter(None, "gpt-4o")

    ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)
    _queue_whisper(store, "wsp_billing_4")
    second = ncp.run(agent=_agent(), turn="go", adapter=adapter, store=store, config=config)

    assert second.cost_source == "memoized"
    assert second.evidence is not None and second.evidence.whisper_ids == []
    assert _pending(store) == ["wsp_billing_4"]
