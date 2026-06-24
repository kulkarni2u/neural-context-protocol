"""Local identity key helpers for NCP."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


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
