import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from shared.auth import otp  # noqa: E402


def test_generated_code_is_six_digits():
    code = otp._generate_code()
    assert len(code) == otp.CODE_LENGTH == 6
    assert code.isdigit()


def test_same_code_hashes_the_same_way():
    assert otp._hash("123456") == otp._hash("123456")
    assert otp._hash("123456") != otp._hash("654321")


def test_email_destination_is_lowercased_and_trimmed():
    assert otp.normalise_destination("  Someone@Example.COM  ", "email") == "someone@example.com"


def test_phone_destination_is_normalised_to_e164_without_plus():
    assert otp.normalise_destination("+91 98765 43210", "call") == "919876543210"
    assert otp.normalise_destination("09876543210", "call") == "919876543210"


def test_invalid_phone_raises_otp_error_not_a_bare_valueerror():
    with pytest.raises(otp.OtpError):
        otp.normalise_destination("not-a-phone", "call")


def test_unknown_channel_is_rejected():
    with pytest.raises(otp.OtpError):
        otp.normalise_destination("someone@example.com", "sms")
