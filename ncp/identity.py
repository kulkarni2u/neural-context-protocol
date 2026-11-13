"""Local identity key helpers for NCP."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


if TYPE_CHECKING:
    from ncp.types import Whisper


def identity_id_for_public_key(public_key: bytes) -> str:
    digest = hashlib.sha256(public_key).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:16]


def generate_ed25519_identity() -> tuple[str, str, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    identity_id = identity_id_for_public_key(public_bytes)
    public_key = base64.b64encode(public_bytes).decode("ascii")
    return identity_id, public_key, private_bytes


def resolve_keystore_dir(project_root: Path) -> Path:
    configured = os.environ.get("NCP_KEYSTORE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ncp" / "keys"


def store_secret_key(identity_id: str, private_key: bytes, *, keystore_dir: Path) -> Path:
    keystore_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    keystore_dir.chmod(0o700)
    path = keystore_dir / f"{identity_id}.key"
    path.write_bytes(base64.b64encode(private_key))
    path.chmod(0o600)
    return path


def load_secret_key(identity_id: str, *, keystore_dir: Path) -> bytes:
    """Load a raw Ed25519 private key from the 0600 keystore file."""

    path = keystore_dir / f"{identity_id}.key"
    return base64.b64decode(path.read_bytes())


def canonical_authorship_payload(
    written_by: str, content: str, pipeline_id: str | None
) -> bytes:
    """Deterministic authorship payload signed/verified on both sides.

    Canonical form is ``written_by | sha256(content) | pipeline_id`` joined with
    ``|``. The content is hashed (not embedded) so the signed payload stays
    bounded regardless of chunk size and identical bytes are produced wherever
    the same triple is supplied.
    """

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return "|".join([written_by, content_hash, pipeline_id or ""]).encode("utf-8")


def canonical_whisper_payload(whisper: Whisper) -> bytes:
    """Version-2 signature envelope, built *before* server thread resolution.

    Construct a ``Whisper`` with the same explicit fields sent to MCP, then
    sign these bytes and pass ``signature_version=2``. The model normalizes
    structured payloads; random server ids/timestamps and the derived thread
    are excluded. The explicit parent/thread/task and every delivery control
    are bound, so a valid signature cannot be redirected or reclassified.
    """
    from ncp.types import normalize_whisper_payload

    # Whisper preserves accepted legacy JSON strings verbatim, while MCP
    # normalizes their defaults and numeric types. Canonicalize typed JSON
    # semantically so this helper works before *and* after MCP normalization
    # without depending on legacy-vs-structured JSON key ordering.
    normalized, _ = normalize_whisper_payload(whisper.whisper_type, whisper.payload)
    if whisper.whisper_type not in {"nudge", "consolidation_ready"}:
        normalized = json.dumps(json.loads(normalized), sort_keys=True, separators=(",", ":"), allow_nan=False)
    envelope = {
        "domain": "ncp:whisper", "version": 2,
        "from": whisper.from_agent, "target": whisper.target,
        "type": whisper.whisper_type,
        "payload_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "pipeline_id": whisper.pipeline_id, "ref": whisper.ref,
        "in_reply_to": whisper.in_reply_to, "thread_id": whisper.thread_id,
        "task_id": whisper.task_id, "confidence": whisper.confidence,
        "ttl_seconds": whisper.ttl_seconds,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sign(payload: bytes | str, *, identity_id: str, keystore_dir: Path) -> str:
    """Sign ``payload`` with the local private key for ``identity_id``.

    Returns a base64-encoded Ed25519 signature string.
    """

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    raw = load_secret_key(identity_id, keystore_dir=keystore_dir)
    private_key = Ed25519PrivateKey.from_private_bytes(raw)
    signature = private_key.sign(payload)
    return base64.b64encode(signature).decode("ascii")


def verify_signature(public_key: str, payload: bytes | str, signature: str) -> bool:
    """Verify a base64 signature over ``payload`` against a base64 public key.

    Returns ``False`` for any malformed input or signature mismatch rather than
    raising, so callers can treat verification as a boolean gate.
    """

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        pub.verify(base64.b64decode(signature), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
