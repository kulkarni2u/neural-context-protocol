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

    def test_structured_whisper_canonical_key_order_has_one_signature_payload(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)
        expected_stored = '{"ask":"review","files":["ncp/types.py"],"slice":"payloads"}'
        canonical = canonical_authorship_payload(identity_id, expected_stored, "pipe_sign")
        signature = sign(canonical, identity_id=identity_id, keystore_dir=keystore)
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

        for payload in (
            {"slice": "payloads", "files": ["ncp/types.py"], "ask": "review"},
            {"ask": "review", "files": ["ncp/types.py"], "slice": "payloads"},
        ):
            resp = _handle_request(
                _call(
                    "ncp_emit_whisper",
                    {
                        "from": identity_id,
                        "target": "reviewer",
                        "type": "share",
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
            assert result["payload_format"] == "structured-v1"
            assert result["verified"] is True

        drained = store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=2
        )
        assert [whisper.payload for whisper in drained] == [expected_stored, expected_stored]

    def test_legacy_json_whisper_canonical_signature_still_verifies(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)
        legacy_payload = '{"ask": "review", "files": ["ncp/types.py"]}'
        expected_stored = '{"slice":null,"files":["ncp/types.py"],"ask":"review"}'
        canonical = canonical_authorship_payload(identity_id, expected_stored, "pipe_sign")
        signature = sign(canonical, identity_id=identity_id, keystore_dir=keystore)
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

        resp = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "share",
                    "payload": legacy_payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": signature,
                },
            ),
            handlers,
        )

        result = _content(resp)
        assert result["emitted"] is True
        assert result["payload_format"] == "legacy"
        assert result["verified"] is True
        drained = store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        )
        assert drained[0].payload == expected_stored

    def test_legacy_alert_json_string_signature_uses_normalized_payload(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)
        legacy_payload = '{"description": "Store unavailable", "alert_code": "store_down"}'
        expected_stored = '{"alert_code":"store_down","description":"Store unavailable"}'
        signature = sign(
            canonical_authorship_payload(identity_id, expected_stored, "pipe_sign"),
            identity_id=identity_id,
            keystore_dir=keystore,
        )
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

        resp = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "alert",
                    "payload": legacy_payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": signature,
                },
            ),
            handlers,
        )

        result = _content(resp)
        assert result.pop("whisper_id").startswith("wsp_")
        assert result == {
            "emitted": True,
            "payload_format": "legacy",
            "verified": True,
        }
        drained = store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        )
        assert drained[0].payload == expected_stored

        raw_signature = sign(
            canonical_authorship_payload(identity_id, legacy_payload, "pipe_sign"),
            identity_id=identity_id,
            keystore_dir=keystore,
        )
        rejected = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "alert",
                    "payload": legacy_payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": raw_signature,
                },
            ),
            handlers,
        )

        error = _error(rejected)
        assert error["code"] == -32603
        assert "require_signatures" in error["message"]
        assert store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        ) == []

    def test_legacy_world_check_json_signature_normalizes_float_before_verifying(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)
        legacy_payload = '{"detected_drift": 0.2500, "anchor_intent": "ship_task_6"}'
        expected_stored = '{"anchor_intent":"ship_task_6","detected_drift":0.25}'
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

        normalized_signature = sign(
            canonical_authorship_payload(identity_id, expected_stored, "pipe_sign"),
            identity_id=identity_id,
            keystore_dir=keystore,
        )
        verified = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "world_check",
                    "payload": legacy_payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": normalized_signature,
                },
            ),
            handlers,
        )

        verified_result = _content(verified)
        assert verified_result.pop("whisper_id").startswith("wsp_")
        assert verified_result == {
            "emitted": True,
            "payload_format": "legacy",
            "verified": True,
        }
        drained = store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        )
        assert drained[0].payload == expected_stored

        source_order_signature = sign(
            canonical_authorship_payload(identity_id, legacy_payload, "pipe_sign"),
            identity_id=identity_id,
            keystore_dir=keystore,
        )
        rejected = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "world_check",
                    "payload": legacy_payload,
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": source_order_signature,
                },
            ),
            handlers,
        )

        error = _error(rejected)
        assert error["code"] == -32603
        assert "require_signatures" in error["message"]
        assert store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        ) == []

    def test_noncanonical_structured_whisper_signature_is_rejected(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        keystore = tmp_path / "keys"
        identity_id = _make_identity(store, keystore)
        noncanonical = '{"slice":"payloads","files":["ncp/types.py"],"ask":"review"}'
        signature = sign(
            canonical_authorship_payload(identity_id, noncanonical, "pipe_sign"),
            identity_id=identity_id,
            keystore_dir=keystore,
        )
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))

        resp = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": identity_id,
                    "target": "reviewer",
                    "type": "share",
                    "payload": {
                        "slice": "payloads",
                        "files": ["ncp/types.py"],
                        "ask": "review",
                    },
                    "confidence": 0.8,
                    "pipeline_id": "pipe_sign",
                    "signature": signature,
                },
            ),
            handlers,
        )

        error = _error(resp)
        assert error["code"] == -32603
        assert "require_signatures" in error["message"]
        assert store.drain_whispers(
            agent_id="reviewer", pipeline_id="pipe_sign", max_items=1
        ) == []


class TestRoutingBoundWhispers:
    def _signed_args(self, tmp_path, store, **overrides):
        from ncp.identity import canonical_whisper_payload
        from ncp.types import Whisper

        identity_id = _make_identity(store, tmp_path / 'keys')
        args = {
            'from': identity_id, 'target': 'reviewer', 'type': 'share',
            'payload': {'ask': 'review'}, 'confidence': 0.8,
            'pipeline_id': 'pipe_sign', 'ttl_seconds': 1800,
            'thread_id': 'thread_original', 'ref': 'chunk_original',
            'in_reply_to': 'parent_original', 'task_id': 'task_original',
        }
        args.update(overrides)
        whisper = Whisper(**{('from_agent' if k == 'from' else 'whisper_type' if k == 'type' else k): v
                             for k, v in args.items()})
        args['signature'] = sign(canonical_whisper_payload(whisper), identity_id=identity_id,
                                 keystore_dir=tmp_path / 'keys')
        args['signature_version'] = 2
        return args

    def test_v2_normalization_and_thread_resolution(self, tmp_path):
        from ncp.identity import canonical_whisper_payload
        from ncp.types import Whisper

        store = SQLiteStore(tmp_path / 'store.db')
        args = self._signed_args(tmp_path, store, thread_id=None, task_id=None,
                                 in_reply_to='parent_gone')
        config = _config(tmp_path, require_signatures=True)
        config.values['identity']['require_routing_signatures'] = True
        result = _content(_handle_request(_call('ncp_emit_whisper', args),
                                         make_handlers(store, config=config)))
        assert result['routing_verified'] is True
        assert result['thread_id'] == 'parent_gone'
        received = store.get_whisper(result['whisper_id'])
        assert received.verified is True
        # Unused ids/timestamps are not part of the request envelope.
        one = Whisper(from_agent='a', target='b', whisper_type='share', payload={'ask': 'review'}, confidence=.8)
        two = Whisper(from_agent='a', target='b', whisper_type='share', payload={'ask': 'review'}, confidence=.8)
        assert canonical_whisper_payload(one) == canonical_whisper_payload(two)

    def test_v2_accepts_structured_and_legacy_json_normalization(self, tmp_path):
        from ncp.identity import canonical_whisper_payload
        from ncp.types import Whisper

        store = SQLiteStore(tmp_path / "store.db")
        identity_id = _make_identity(store, tmp_path / "keys")
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))
        pairs = [
            ("share", {"ask": "review"}, '{"ask": "review"}'),
            ("alert", {"alert_code": "store_down", "description": "Store unavailable"},
             '{"description":"Store unavailable","alert_code":"store_down"}'),
            ("world_check", {"anchor_intent": "ship", "detected_drift": 0.25},
             '{"detected_drift":0.2500,"anchor_intent":"ship"}'),
        ]
        for whisper_type, structured, legacy in pairs:
            envelopes = []
            for payload in (structured, legacy):
                args = {"from": identity_id, "target": "reviewer", "type": whisper_type,
                        "payload": payload, "confidence": .8, "pipeline_id": "pipe_sign",
                        "ttl_seconds": 1800}
                whisper = Whisper(from_agent=identity_id, target="reviewer", whisper_type=whisper_type,
                                  payload=payload, confidence=.8, pipeline_id="pipe_sign",
                                  ttl_seconds=1800)
                envelope = canonical_whisper_payload(whisper)
                envelopes.append(envelope)
                args.update(signature=sign(envelope, identity_id=identity_id,
                                           keystore_dir=tmp_path / "keys"), signature_version=2)
                result = _content(_handle_request(_call("ncp_emit_whisper", args), handlers))
                assert result["routing_verified"] is True
            assert envelopes[0] == envelopes[1]

    def test_v2_requires_explicit_positive_int_ttl(self, tmp_path):
        store = SQLiteStore(tmp_path / 'store.db')
        args = self._signed_args(tmp_path, store)
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))
        missing = {k: v for k, v in args.items() if k != 'ttl_seconds'}
        for bad in (missing, {**args, 'ttl_seconds': 0}, {**args, 'ttl_seconds': -5},
                    {**args, 'ttl_seconds': True}, {**args, 'ttl_seconds': '60'},
                    {**args, 'ttl_seconds': 1.5}):
            message = _error(_handle_request(_call('ncp_emit_whisper', bad), handlers))['message']
            assert 'explicit positive integer ttl_seconds' in message
        assert not store.drain_whispers(agent_id='reviewer', pipeline_id='pipe_sign', max_items=100)

    def test_v2_explicit_ttl_verifies_with_custom_default_ttl(self, tmp_path):
        store = SQLiteStore(tmp_path / 'store.db')
        args = self._signed_args(tmp_path, store, ttl_seconds=90)
        config = _config(tmp_path, require_signatures=True)
        config.values['whispers']['default_ttl_seconds'] = 600
        result = _content(_handle_request(_call('ncp_emit_whisper', args),
                                         make_handlers(store, config=config)))
        assert result['routing_verified'] is True
        assert store.get_whisper(result['whisper_id']).ttl_seconds == 90

    def test_v2_rejects_tampering_before_persisting(self, tmp_path):
        store = SQLiteStore(tmp_path / 'store.db')
        args = self._signed_args(tmp_path, store)
        handlers = make_handlers(store, config=_config(tmp_path, require_signatures=True))
        for field, replacement in {
            'from': 'attacker', 'target': 'attacker', 'type': 'request',
            'payload': {'ask': 'altered'}, 'pipeline_id': 'other', 'ref': 'other_chunk',
            'thread_id': 'other_thread', 'in_reply_to': 'other_parent',
            'task_id': 'other_task', 'confidence': .2, 'ttl_seconds': 1,
        }.items():
            result = _error(_handle_request(_call('ncp_emit_whisper', {**args, field: replacement}), handlers))
            assert 'could not be verified' in result['message'], field
        assert not store.drain_whispers(agent_id='reviewer', pipeline_id='pipe_sign', max_items=100)

    def test_routing_required_rejects_legacy_and_unsigned(self, tmp_path):
        store = SQLiteStore(tmp_path / 'store.db')
        args = self._signed_args(tmp_path, store)
        config = _config(tmp_path, require_signatures=False)
        config.values['identity']['require_routing_signatures'] = True
        handlers = make_handlers(store, config=config)
        assert 'requires signature_version=2' in _error(_handle_request(
            _call('ncp_emit_whisper', {**args, 'signature_version': 1}), handlers))['message']
        unsigned = {key: value for key, value in args.items() if key != 'signature'}
        assert 'could not be verified' in _error(_handle_request(
            _call('ncp_emit_whisper', unsigned), handlers))['message']

    def test_legacy_reports_unverified_routing(self, tmp_path):
        store = SQLiteStore(tmp_path / 'store.db')
        signer = _make_identity(store, tmp_path / 'keys')
        signature = sign(canonical_authorship_payload(signer, 'hello', 'pipe_sign'),
                         identity_id=signer, keystore_dir=tmp_path / 'keys')
        result = _content(_handle_request(_call('ncp_emit_whisper', {
            'from': signer, 'target': 'reviewer', 'type': 'nudge', 'payload': 'hello',
            'confidence': .8, 'pipeline_id': 'pipe_sign', 'signature': signature, 'signature_version': 1,
        }), make_handlers(store, config=_config(tmp_path, require_signatures=True))))
        assert result['verified'] is True
        assert result['routing_verified'] is False
        assert result['signature_version'] == 1


def test_emit_whisper_rejects_non_string_pipeline_id_before_persisting(tmp_path):
    store = SQLiteStore(tmp_path / 'store.db')
    handlers = make_handlers(store, config=_config(tmp_path, require_signatures=False))
    args = {'from': 'a', 'target': 'reviewer', 'type': 'request', 'payload': {'ask': 'x'},
            'confidence': 0.9}
    for bad in (7, 1.5, True, ['p'], {'p': 1}):
        message = _error(_handle_request(_call('ncp_emit_whisper', {**args, 'pipeline_id': bad}), handlers))['message']
        assert 'ncp_emit_whisper pipeline_id must be a string' in message
    assert not store.drain_whispers(agent_id='reviewer', pipeline_id='7', max_items=100)
    assert not store.drain_whispers(agent_id='reviewer', pipeline_id=None, max_items=100)
