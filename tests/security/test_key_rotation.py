"""Rotating ENCRYPTION_KEY: old values stay readable during the change, and the script re-encrypts them all."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from cryptography.fernet import Fernet  # noqa: E402

from scripts import rotate_encryption_key as rot  # noqa: E402
from shared.auth import encryption  # noqa: E402
from shared.config.settings import settings  # noqa: E402

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


def _keys(monkeypatch, current, old=""):
    monkeypatch.setattr(settings, "encryption_key", current)
    monkeypatch.setattr(settings, "encryption_keys_old", old)
    encryption.reset_cipher()


def test_a_rotation_never_breaks_what_is_stored(monkeypatch):
    _keys(monkeypatch, OLD)
    stored = encryption.encrypt("whatsapp-token")
    _keys(monkeypatch, NEW, OLD)  # step 2: new key, old one kept for reading
    assert encryption.decrypt(stored) == "whatsapp-token"
    fresh = encryption.encrypt("new-token")
    assert Fernet(NEW.encode()).decrypt(fresh.encode()) == b"new-token"  # new writes use the new key
    _keys(monkeypatch, NEW)  # old key dropped too early
    try:
        encryption.decrypt(stored)
        raise AssertionError("an old value must not open without its key")
    except encryption.DecryptionError:
        pass
    encryption.reset_cipher()


def test_the_script_finds_tokens_anywhere_and_only_those(monkeypatch):
    _keys(monkeypatch, OLD)
    a, b = encryption.encrypt("token-a"), encryption.encrypt("pin-1234")
    _keys(monkeypatch, NEW, OLD)
    current = Fernet(NEW.encode())
    stats = {"rotated": 0, "failed": 0}
    value = {"access": a, "extra": {"two_step_pin": b, "list": [a, "plain text", 42]}, "name": "Sharma Clinic"}
    out = rot.rotate_value(value, current, stats)
    assert stats == {"rotated": 3, "failed": 0}
    assert out["name"] == "Sharma Clinic" and out["extra"]["list"][1:] == ["plain text", 42]
    assert current.decrypt(out["access"].encode()) == b"token-a"
    assert current.decrypt(out["extra"]["two_step_pin"].encode()) == b"pin-1234"
    again = {"rotated": 0, "failed": 0}
    assert rot.rotate_value(out, current, again) == out and again == {"rotated": 0, "failed": 0}  # nothing left
    encryption.reset_cipher()


def test_a_token_no_key_opens_is_reported_not_destroyed(monkeypatch):
    stray = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    _keys(monkeypatch, NEW, OLD)
    stats = {"rotated": 0, "failed": 0}
    assert rot.rotate_value(stray, Fernet(NEW.encode()), stats) == stray and stats["failed"] == 1
    encryption.reset_cipher()
