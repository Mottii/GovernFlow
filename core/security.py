import hashlib
import os
import secrets
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_virtual_key() -> str:
    return "gk_" + secrets.token_urlsafe(24)


def virtual_key_hash(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def master_key() -> bytes:
    from core.keyrings import active_id, key

    return key("encryption", active_id("encryption"))


def credential_aad(client_id: uuid.UUID, provider: str, label: str) -> bytes:
    return f"{client_id}:{provider}:{label}".encode()


def encrypt_provider_key(
    plaintext: str, client_id: uuid.UUID, provider: str, label: str, *, key_id: str | None = None
) -> tuple[bytes, bytes]:
    from core.keyrings import active_id, key

    identity = key_id or active_id("encryption")
    nonce = os.urandom(12)
    aad = credential_aad(client_id, provider, label)
    if identity != "0000":
        aad = b"v2:" + identity.encode() + b":" + aad
    ciphertext = AESGCM(key("encryption", identity)).encrypt(nonce, plaintext.encode(), aad)
    return ciphertext, nonce


def decrypt_provider_key(
    ciphertext: bytes,
    nonce: bytes,
    client_id: uuid.UUID,
    provider: str,
    label: str,
    *,
    key_id: str | None = None,
) -> str:
    from core.keyrings import key

    aad = credential_aad(client_id, provider, label)
    if key_id and key_id != "0000":
        aad = b"v2:" + key_id.encode() + b":" + aad
    plaintext = AESGCM(key("encryption", key_id)).decrypt(nonce, ciphertext, aad)
    return plaintext.decode()
