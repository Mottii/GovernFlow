import base64
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
    encoded = os.environ.get("MASTER_ENCRYPTION_KEY", "")
    try:
        value = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must be base64") from exc
    if len(value) != 32:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must decode to 32 bytes")
    return value


def credential_aad(client_id: uuid.UUID, provider: str, label: str) -> bytes:
    return f"{client_id}:{provider}:{label}".encode()


def encrypt_provider_key(
    plaintext: str, client_id: uuid.UUID, provider: str, label: str
) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(master_key()).encrypt(
        nonce, plaintext.encode(), credential_aad(client_id, provider, label)
    )
    return ciphertext, nonce


def decrypt_provider_key(
    ciphertext: bytes, nonce: bytes, client_id: uuid.UUID, provider: str, label: str
) -> str:
    plaintext = AESGCM(master_key()).decrypt(
        nonce, ciphertext, credential_aad(client_id, provider, label)
    )
    return plaintext.decode()
