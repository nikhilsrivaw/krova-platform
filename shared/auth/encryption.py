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

from cryptography.fernet import Fernet, InvalidToken

from shared.config.settings import settings


class DecryptionError(Exception):
    """
    Raised when stored ciphertext cannot be read.

    In practice this almost always means ENCRYPTION_KEY changed - a rotated or
    regenerated key makes every stored credential unreadable at once. Worth
    recognising quickly, because the symptom (every channel suddenly failing)
    looks nothing like the cause.
    """


_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        if not settings.encryption_key:
            raise RuntimeError("ENCRYPTION_KEY is not set")
        try:
            _fernet = Fernet(settings.encryption_key.encode())
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


def encrypt_optional(plaintext: str | None) -> str | None:
    return encrypt(plaintext) if plaintext else None


def decrypt_optional(ciphertext: str | None) -> str | None:
    return decrypt(ciphertext) if ciphertext else None
