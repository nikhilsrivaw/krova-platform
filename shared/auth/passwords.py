"""
Password hashing.

Argon2id, which is what the Password Hashing Competition settled on and what
OWASP now recommends over bcrypt. The parameters below are the argon2-cffi
defaults, which track current guidance; they are named explicitly so that
changing them later is a deliberate act with a visible diff.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,        # iterations
    memory_cost=65536,  # 64 MiB - the part that makes GPU attacks expensive
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Long enough to be sane, short enough that nobody can send us a 10 MB password
# to burn 64 MiB of memory hashing.
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256


class PasswordTooWeak(ValueError):
    """Raised when a password fails the minimum policy."""


def validate_password(password: str) -> None:
    """
    Check a password before hashing it.

    Length is the only rule. Composition rules - one capital, one symbol -
    push people toward Password1! and measurably weaken what they choose, so
    we ask for length instead.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooWeak(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordTooWeak(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters"
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """
    Check a password against a stored hash.

    Returns False rather than raising on a mismatch: a wrong password is an
    ordinary event, not an exception.
    """
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(password_hash: str) -> bool:
    """
    True if this hash was made with weaker parameters than we now use.

    Call it after a successful login: that is the only moment we hold the
    plaintext and can transparently upgrade the stored hash.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False
