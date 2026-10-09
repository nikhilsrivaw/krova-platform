"""
A one-time code sent to prove ownership of an email address or phone
number, for passwordless login/registration (shared/auth/otp.py) - not a
business's own customer-facing OTP (Shopify COD confirmation etc. use
their own, separate mechanisms).

One row per code sent, not one row per destination kept updated in place:
a history of attempts is what lets rate-limiting ("already sent one
recently") and brute-force limiting ("too many wrong guesses on this one")
both work off a simple query, and a stale, unconsumed, long-expired row
costs nothing left lying around.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class OtpCode(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "otp_codes"

    # An email address (lowercased) or an E.164 phone number - whichever
    # channel below sends to.
    destination: Mapped[str] = mapped_column(String(320), nullable=False)
    # "email" | "call" - call always reads the code aloud over a voice
    # call, never SMS (shared/auth/otp.py's own docstring on why).
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    # Compared against on every verify attempt - never store the plaintext
    # code itself here.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only set for channel="call": the code, reversibly encrypted
    # (shared/auth/encryption.py - same mechanism already used for
    # recoverable webhook tokens elsewhere in this codebase), so the
    # outbound call's own answer webhook can recover and read it aloud.
    # Plivo gives no call_uuid until the call is actually placed, so
    # there is nowhere else to stash the plaintext between "send the call"
    # and "the call connects" other than a column keyed by this row's own
    # id, passed in the answer_url's query string.
    code_enc: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_otp_codes_destination_channel", "destination", "channel", "created_at"),
    )
