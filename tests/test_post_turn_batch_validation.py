"""ncp_post_turn memory_chunks get the same write checks as ncp_write_memory.

Regression: the batch path skipped authorship verification (so
require_signatures was bypassable), skipped CAP-T2 grounding (so an
ungrounded tool_result claim kept full trust) and dropped provenance.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ncp.config import load_config
from ncp.identity import canonical_authorship_payload, generate_ed25519_identity, sign, store_secret_key
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

PIPELINE = "pipe_batch"


def _post_turn(handlers, chunks: list[dict], **extra: object) -> dict:
    args = {
        "agent_id": "executor",
        "role": "build",
        "task": "batch_validation",
        "slot": "post_turn",
        "intent": "write_memory",
        "pipeline_id": PIPELINE,
        "turn_id": "turn_batch_1",
        "result_summary": "done",
        "result_full": "done in full",
        "memory_chunks": chunks,
        **extra,
    }
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ncp_post_turn", "arguments": args}}
    return json.loads(_handle_request(request, handlers))


def _result(response: dict) -> dict:
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _config(tmp_path: Path, **identity: bool):
    config = load_config(cwd=tmp_path)
    config.values["identity"].update(identity)
    return config


def _identity(store: SQLiteStore, keys: Path) -> str:
    identity_id, public_key, private_key = generate_ed25519_identity()
    store_secret_key(identity_id, private_key, keystore_dir=keys)
    store.register_identity(identity_id=identity_id, public_key=public_key, label="batch")
    return identity_id


def _row(store: SQLiteStore, chunk_id: str):
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()


def _turn_count(store: SQLiteStore) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0])


def _signed(identity_id: str, keys: Path, chunk_id: str, content: str) -> dict:
    payload = canonical_authorship_payload(identity_id, content, PIPELINE)
    return {
        "chunk_id": chunk_id, "content": content, "layer": "semantic", "src": "agent_inferred",
        "written_by": identity_id, "signature": sign(payload, identity_id=identity_id, keystore_dir=keys),
    }


def test_signed_batch_chunk_is_marked_verified(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    keys = tmp_path / "keys"
    identity_id = _identity(store, keys)
    handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

    _result(_post_turn(handlers, [_signed(identity_id, keys, "batch_ok", "signed batch fact about retries")]))

    row = _row(store, "batch_ok")
    assert row is not None and bool(row["verified"]) is True


def test_mixed_validity_batch_is_rejected_atomically(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    keys = tmp_path / "keys"
    identity_id = _identity(store, keys)
    handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

    good = _signed(identity_id, keys, "batch_good", "signed batch fact about retries")
    forged = {**_signed(identity_id, keys, "batch_forged", "another fact"), "content": "tampered fact"}
    response = _post_turn(handlers, [good, forged])

    assert "require_signatures" in response["error"]["message"]
    assert _row(store, "batch_good") is None
    assert _row(store, "batch_forged") is None
    assert _turn_count(store) == 0


def test_unsigned_batch_still_works_by_default(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    handlers = make_handlers(store, config=_config(tmp_path))

    _result(_post_turn(handlers, [{
        "chunk_id": "batch_plain", "content": "plain unsigned fact", "layer": "semantic", "src": "agent_inferred",
    }]))

    row = _row(store, "batch_plain")
    assert row is not None and bool(row["verified"]) is False


def test_ungrounded_high_trust_batch_claim_is_demoted(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    handlers = make_handlers(store, config=_config(tmp_path, require_grounded_high_trust=True))

    result = _result(_post_turn(handlers, [{
        "chunk_id": "batch_claim", "content": "the deploy passed all checks",
        "layer": "semantic", "src": "tool_result",
    }]))

    assert result["trust_demoted_chunk_ids"] == ["batch_claim"]
    assert float(_row(store, "batch_claim")["base_trust"]) <= 0.60


def test_grounded_batch_claim_keeps_trust_and_provenance(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(SubconsciousChunk(
        chunk_id="evidence_log", layer="episodic", content="ci log: 212 passed",
        src="tool_result", pipeline_id=PIPELINE, written_by="ci",
    ))
    handlers = make_handlers(store, config=_config(tmp_path, require_grounded_high_trust=True))

    result = _result(_post_turn(handlers, [{
        "chunk_id": "batch_grounded", "content": "the deploy passed all checks",
        "layer": "semantic", "src": "tool_result",
        "evidence_id": "evidence_log", "caused_by": "evidence_log",
    }]))

    assert "trust_demoted_chunk_ids" not in result
    row = _row(store, "batch_grounded")
    assert float(row["base_trust"]) == 0.80
    assert row["evidence_id"] == "evidence_log"
    assert row["caused_by"] == "evidence_log"


def test_cross_pipeline_evidence_does_not_ground_batch_claim(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(SubconsciousChunk(
        chunk_id="foreign_log", layer="episodic", content="ci log elsewhere",
        src="tool_result", pipeline_id="other_pipe", written_by="ci",
    ))
    handlers = make_handlers(store, config=_config(tmp_path, require_grounded_high_trust=True))

    result = _result(_post_turn(handlers, [{
        "chunk_id": "batch_foreign", "content": "the deploy passed all checks",
        "layer": "semantic", "src": "tool_result", "evidence_id": "foreign_log",
    }]))

    assert result["trust_demoted_chunk_ids"] == ["batch_foreign"]
