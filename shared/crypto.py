"""Symmetric encryption for stored secret payloads.

Shared so the API (which encrypts on write) and the worker (which decrypts at
the moment of use) agree on the format and on the key-handling rules without
importing each other.

The master key comes from the environment — preferably
``OPSPILOT_MASTER_KEY_FILE``, so it is not stored next to the ciphertext it
protects. :meth:`shared.config.Settings.resolve_master_key` refuses
group/world-readable key files.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from shared.config import Settings, get_settings

__all__ = ["CryptoError", "decrypt_secret", "encrypt_secret", "fernet"]


class CryptoError(RuntimeError):
    """The payload could not be decrypted, or the key is unusable."""


def fernet(settings: Settings | None = None) -> Fernet:
    """Build the Fernet from the configured master key."""
    cfg = settings or get_settings()
    raw = cfg.resolve_master_key()
    try:
        return Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as exc:
        raise CryptoError(
            "master key is not a valid Fernet key; generate one with "
            "cryptography.fernet.Fernet.generate_key()"
        ) from exc


def encrypt_secret(plaintext: str, settings: Settings | None = None) -> str:
    """Encrypt for storage. The returned string is safe to put in a column."""
    if not plaintext:
        raise CryptoError("refusing to encrypt an empty secret")
    return fernet(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, settings: Settings | None = None) -> str:
    """Decrypt at the moment of use.

    The caller is responsible for dropping the result as soon as it is done.
    Nothing here logs, returns, or persists the plaintext beyond this call.
    """
    if not ciphertext:
        raise CryptoError("empty ciphertext")
    try:
        return fernet(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError(
            "payload did not decrypt: wrong master key, or the row was tampered with"
        ) from exc
