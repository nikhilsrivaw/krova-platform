"""
Encryption for credentials held at rest.

Channel access tokens are the keys to a customer's WhatsApp account, their
mailbox, their Instagram. A database backup that leaks them is not a data
breach on our side - it is a breach on theirs, on every business at once.
So they are encrypted with a key that lives in the environment rather than
the database, and a dump of one without the other is inert.

Fernet: AES-128-CBC with an HMAC, timestamped, authenticated. Not the fastest
option available, and it does not need to be - this runs when a channel is
connected or a token refreshed, not on the request path.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from shared.config.settings import settings


class DecryptionError(Exception):
    """
    Raised when stored ciphertext cannot be read.

    In practice this almost always means ENCRYPTION_KEY changed - a rotated or
    regenerated key makes every stored credential unreadable at once. Worth
    recognising quickly, because the symptom (every channel suddenly failing)
    looks nothing like the cause.
    """


_fernet: MultiFernet | None = None


def _cipher() -> MultiFernet:
    """
    The current key first - it encrypts everything new - then any earlier keys from ENCRYPTION_KEYS_OLD, which are
    only ever used to read what was stored before a rotation (scripts/rotate_encryption_key.py).
    """
    global _fernet
    if _fernet is None:
        if not settings.encryption_key:
            raise RuntimeError("ENCRYPTION_KEY is not set")
        try:
            keys = [settings.encryption_key] + [k.strip() for k in settings.encryption_keys_old.split(",") if k.strip()]
            _fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a credential for storage."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Read a stored credential back."""
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted - ENCRYPTION_KEY may have changed"
        ) from exc


def rotate(ciphertext: str) -> str:
    """The same secret encrypted again with the current key (it may have been stored with an earlier one)."""
    try:
        return _cipher().rotate(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Stored credential could not be decrypted with any configured key") from exc


def reset_cipher() -> None:
    """Forget the cached keys (tests, and a key change inside one process)."""
    global _fernet
    _fernet = None


def encrypt_optional(plaintext: str | None) -> str | None:
    return encrypt(plaintext) if plaintext else None


def decrypt_optional(ciphertext: str | None) -> str | None:
    return decrypt(ciphertext) if ciphertext else None
