"""R3: debounced auto-calibration after ncp_record_outcome; self_report_weight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ncp.config import load_config
from ncp.mcp.server import make_handlers
from ncp.stores.calibration import compute_outcome_evidence
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord, SubconsciousChunk


def _setup(tmp_path: Path, **calibration: Any):
    config = load_config(cwd=tmp_path, env={})
    config.values["calibration"].update(calibration)
    store = SQLiteStore(tmp_path / "ncp.db")
    for cid, pipe in (("c1", "p1"), ("c2", "p2")):
        store.write(SubconsciousChunk(
            chunk_id=cid, layer="semantic", content=f"auto calibrate subject {cid}", src="tool_result",
            pipeline_id=pipe, written_by="author", base_trust=0.5,
        ))
    return store, config


def _trust(store: SQLiteStore, cid: str) -> float:
    with store._connect() as c:
        return float(c.execute("SELECT base_trust FROM chunks WHERE chunk_id=?", (cid,)).fetchone()[0])


def _spy(store: SQLiteStore) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = store.calibrate

    def wrapper(**kwargs: Any):
        calls.append(kwargs)
        return real(**kwargs)

    store.calibrate = wrapper  # type: ignore[method-assign]
    return calls


def test_two_outcomes_within_interval_calibrate_once(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, auto_min_interval_seconds=3600)
    calls = _spy(store)
    h = make_handlers(store, config=config)
    first = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    second = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    assert first["calibrated"] is True and second["calibrated"] is False
    assert len(calls) == 1
    assert calls[0]["feedback_mode"] is True and calls[0]["pipeline_id"] == "p1"
    assert _trust(store, "c1") > 0.5


def test_debounce_is_per_pipeline(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, auto_min_interval_seconds=3600)
    calls = _spy(store)
    h = make_handlers(store, config=config)
    h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    other = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c2"], "pipeline_id": "p2"})
    assert other["calibrated"] is True and len(calls) == 2


def test_interval_elapsed_calibrates_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, config = _setup(tmp_path, auto_min_interval_seconds=60)
    calls = _spy(store)
    h = make_handlers(store, config=config)
    clock = [1000.0]
    monkeypatch.setattr("ncp.mcp.server.time.monotonic", lambda: clock[0])
    h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    clock[0] += 30
    assert h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})["calibrated"] is False
    clock[0] += 31
    assert h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})["calibrated"] is True
    assert len(calls) == 2


def test_disabled_never_calibrates(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, auto_on_outcome=False)
    calls = _spy(store)
    h = make_handlers(store, config=config)
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    assert res["calibrated"] is False and calls == [] and _trust(store, "c1") == 0.5


def test_failure_is_logged_not_raised(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store, config = _setup(tmp_path)

    def boom(**_: Any):
        raise RuntimeError("db exploded")

    store.calibrate = boom  # type: ignore[method-assign]
    h = make_handlers(store, config=config)
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    assert res["recorded"] is True and res["calibrated"] is False
    assert "auto-calibration failed" in capsys.readouterr().err


def test_not_implemented_calibrate_is_skipped(tmp_path: Path) -> None:
    store, config = _setup(tmp_path)

    def unsupported(**_: Any):
        raise NotImplementedError

    store.calibrate = unsupported  # type: ignore[method-assign]
    h = make_handlers(store, config=config)
    assert h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"]})["calibrated"] is False


def test_replayed_outcome_does_not_calibrate(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, auto_min_interval_seconds=0)
    calls = _spy(store)
    h = make_handlers(store, config=config)
    h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "outcome_id": "same"})
    replay = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "outcome_id": "same"})
    assert replay["recorded"] is False and replay["calibrated"] is False and len(calls) == 1


def test_auto_calibration_does_not_batch_decay(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, auto_min_interval_seconds=0)
    old = SubconsciousChunk(
        chunk_id="old", layer="semantic", content="aged generation zero chunk", src="tool_result",
        pipeline_id="p1", written_by="author", base_trust=0.9,
    )
    store.write(old)
    with store._connect() as c:
        c.execute("UPDATE chunks SET created_at = 1.0 WHERE chunk_id = 'old'")
    h = make_handlers(store, config=config)
    h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "pipeline_id": "p1"})
    assert _trust(store, "old") == pytest.approx(0.9)


def test_self_report_weight_scales_evidence() -> None:
    outs = [
        OutcomeRecord(outcome_id="a", chunk_ids=["c"], success=True, weight=1.0),
        OutcomeRecord(outcome_id="b", chunk_ids=["c"], success=True, weight=1.0, eval_source="verifier"),
        OutcomeRecord(outcome_id="d", chunk_ids=["c"], success=False, weight=0.5, eval_source="human"),
    ]
    assert compute_outcome_evidence(outs)["c"] == pytest.approx(1.5)
    assert compute_outcome_evidence(outs, self_report_weight=0.0)["c"] == pytest.approx(0.5)
    assert compute_outcome_evidence(outs, self_report_weight=0.5)["c"] == pytest.approx(1.0)


def test_self_report_weight_effect_in_calibrate(tmp_path: Path) -> None:
    def run(weight: float, source: str) -> float:
        sub = tmp_path / f"{weight}-{source}"
        sub.mkdir()
        store, config = _setup(sub)
        config.values["calibration"]["self_report_weight"] = weight
        store.config = config
        store.record_outcome(OutcomeRecord(
            outcome_id="o", chunk_ids=["c1"], success=True, weight=0.5, eval_source=source,  # type: ignore[arg-type]
        ))
        store.calibrate(feedback_mode=True, pipeline_id="p1")
        return _trust(store, "c1")

    full = run(1.0, "self_report")
    half = run(0.5, "self_report")
    none = run(0.0, "self_report")
    verifier = run(0.0, "verifier")
    assert full > half > none
    assert none == pytest.approx(0.5, abs=1e-9)
    assert verifier == pytest.approx(full)


def test_config_accessors_clamp(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.calibration_auto_on_outcome is True
    assert config.calibration_auto_min_interval_seconds == 60.0
    assert config.calibration_self_report_weight == 1.0
    config.values["calibration"]["self_report_weight"] = 7
    assert config.calibration_self_report_weight == 1.0
    config.values["calibration"]["self_report_weight"] = "x"
    assert config.calibration_self_report_weight == 1.0
    config.values["calibration"]["auto_min_interval_seconds"] = -5
    assert config.calibration_auto_min_interval_seconds == 60.0
