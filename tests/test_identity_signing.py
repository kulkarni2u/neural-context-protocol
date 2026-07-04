"""CAP-T1 / WI-013: opt-in Ed25519 signing and verification of authorship.

These tests exercise the real keystore + store identity APIs (no mocks) to prove:
  * a valid signature verifies and the chunk is marked verified=True;
  * a forged signature is rejected when require_signatures is on, and merely
    marked unverified when it is off;
  * a revoked identity's signed write is rejected when require_signatures is on;
  * an UNSIGNED write still succeeds under the default (require_signatures off).
"""

from __future__ import annotations

import json
from pathlib import Path

from ncp.config import load_config
from ncp.identity import (
    canonical_authorship_payload,
    generate_ed25519_identity,
    sign,
    store_secret_key,
)
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore


def _call(name: str, arguments: dict, req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _content(response_str: str) -> dict:
    result = json.loads(response_str)["result"]
    return json.loads(result["content"][0]["text"])


def _error(response_str: str) -> dict:
    return json.loads(response_str).get("error", {})


def _make_identity(store: SQLiteStore, keystore_dir: Path, *, label: str = "signer") -> str:
    """Create a real Ed25519 identity via the keystore + store APIs."""
    identity_id, public_key, private_key = generate_ed25519_identity()
    store_secret_key(identity_id, private_key, keystore_dir=keystore_dir)
    store.register_identity(identity_id=identity_id, public_key=public_key, label=label)
    return identity_id


def _config(tmp_path: Path, *, require_signatures: bool):
    config = load_config(cwd=tmp_path)
    config.values["identity"]["require_signatures"] = require_signatures
    return config


class TestSignedAuthorship:
    def test_valid_signature_verifies_and_marks_verified(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)

        content = "signed durable memory chunk"
        pipeline_id = "pipe_sign"
        payload = canonical_authorship_payload(identity_id, content, pipeline_id)
        signature = sign(payload, identity_id=identity_id, keystore_dir=keystore)

        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=False))
        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {
                    "content": content,
                    "layer": "semantic",
                    "src": "tool_result",
                    "written_by": identity_id,
                    "pipeline_id": pipeline_id,
                    "signature": signature,
                },
            ),
            handlers,
        )
        result = _content(resp)
        assert result["written"] is True
        assert result["verified"] is True

        stored = store.query(content, k=4, min_score=0.0, pipeline_id=pipeline_id)
        assert stored, "chunk should be retrievable"
        assert stored[0].verified is True

    def test_forged_signature_rejected_when_required_and_unverified_when_not(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)

        content = "forged authorship attempt"
        forged = "AAAA" + "b" * 84  # well-formed base64 length, invalid signature

        # require_signatures OFF: write succeeds but is marked unverified.
        handlers_off = make_handlers(store, config=_config(tmp_path, require_signatures=False))
        resp_off = _handle_request(
            _call(
                "ncp_write_memory",
                {
                    "content": content,
                    "layer": "semantic",
                    "src": "tool_result",
                    "written_by": identity_id,
                    "signature": forged,
                },
            ),
            handlers_off,
        )
        result_off = _content(resp_off)
        assert result_off["written"] is True
        assert result_off["verified"] is False

        # require_signatures ON: the forged write is rejected outright.
        handlers_on = make_handlers(store, config=_config(tmp_path, require_signatures=True))
        resp_on = _handle_request(
            _call(
                "ncp_write_memory",
                {
                    "content": content,
                    "layer": "semantic",
                    "src": "tool_result",
                    "written_by": identity_id,
                    "signature": forged,
                },
            ),
            handlers_on,
        )
        error = _error(resp_on)
        assert error.get("code") == -32603
        assert "require_signatures" in error.get("message", "")

    def test_revoked_identity_signed_write_rejected_when_required(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)

        content = "write from a revoked identity"
        payload = canonical_authorship_payload(identity_id, content, None)
        signature = sign(payload, identity_id=identity_id, keystore_dir=keystore)

        assert store.revoke_identity(identity_id) is True
        # A valid signature no longer verifies once the identity is revoked.
        assert store.verify_authorship(identity_id, payload, signature) is False

        handlers_on = make_handlers(store, config=_config(tmp_path, require_signatures=True))
        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {
                    "content": content,
                    "layer": "semantic",
                    "src": "tool_result",
                    "written_by": identity_id,
                    "signature": signature,
                },
            ),
            handlers_on,
        )
        error = _error(resp)
        assert error.get("code") == -32603
        assert "require_signatures" in error.get("message", "")

    def test_unsigned_write_still_succeeds_by_default(self, tmp_path: Path) -> None:
        """Compatibility guarantee: unsigned writes keep working with the default."""
        store = SQLiteStore(tmp_path / "store.db")
        config = _config(tmp_path, require_signatures=False)
        assert config.require_signatures is False  # default value

        handlers = make_handlers(store, config=config)
        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {
                    "content": "legacy unsigned chunk",
                    "layer": "semantic",
                    "src": "tool_result",
                    "written_by": "legacy_agent",
                },
            ),
            handlers,
        )
        result = _content(resp)
        assert result["written"] is True
        assert "verified" not in result  # no signature supplied, no verification noise

        stored = store.query("legacy unsigned chunk", k=4, min_score=0.0)
        assert stored
        assert stored[0].verified is False

    def test_signed_whisper_is_verified(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)

        payload = "heads up on the payment slot"
        canonical = canonical_authorship_payload(identity_id, payload, "pipe_sign")
        signature = sign(canonical, identity_id=identity_id, keystore_dir=keystore)

        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=False))
        resp = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "*",
                    "type": "nudge",
                    "payload": payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": signature,
                },
            ),
            handlers,
        )
        result = _content(resp)
        assert result["emitted"] is True
        assert result["verified"] is True
