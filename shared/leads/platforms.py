"""
The listing platforms that send leads to KROVA through a webhook URL, and the
steps a business follows to connect each one.

Two kinds of setup, shown to the business as they are:
- "account_manager": the platform's own team activates the webhook. Their
  typical turnaround is given where known. Real leads only start after that.
- "self_serve": the business pastes the URL in the tool's own settings.

Justdial and IndiaMART have dedicated routers with their own field mapping,
so they are not listed here. The entries below use the shared generic parser
(shared/leads/justdial_parse.py), whose field names are assumptions until a
live payload is seen. Each lead's raw payload is stored for that reason.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LeadPlatform:
    key: str
    label: str
    setup: str  # "account_manager" | "self_serve"
    steps: tuple[str, ...]

    @property
    def token_setting(self) -> str:
        return f"{self.key}_token_hash"

    @property
    def enc_setting(self) -> str:
        # The token stored reversibly, alongside the hash above - see
        # shared/leads/intake.py's encrypt_token/decrypt_token docstrings.
        return f"{self.key}_token_enc"


PLATFORMS: tuple[LeadPlatform, ...] = (
    LeadPlatform(
        key="magicbricks",
        label="Magicbricks",
        setup="account_manager",
        steps=(
            "Copy the URL from this page.",
            "Send it to your Magicbricks account manager and ask them to set it as your lead webhook.",
            "Magicbricks activates it, usually within about a week.",
            "The first real lead shows up in Recent leads below.",
        ),
    ),
    LeadPlatform(
        key="99acres",
        label="99Acres",
        setup="account_manager",
        steps=(
            "Copy the URL from this page.",
            "Send it to your 99Acres account manager and ask them to set it as your lead webhook.",
            "99Acres activates it, usually within about a week.",
            "The first real lead shows up in Recent leads below.",
        ),
    ),
    LeadPlatform(
        key="housing",
        label="Housing.com",
        setup="account_manager",
        steps=(
            "Copy the URL from this page.",
            "Send it to your Housing.com account manager and ask them to set it as your lead webhook.",
            "Housing.com activates it, usually within about a week.",
            "The first real lead shows up in Recent leads below.",
        ),
    ),
    LeadPlatform(
        key="generic",
        label="Any other tool (form, CRM, automation)",
        setup="self_serve",
        steps=(
            "Copy the URL from this page.",
            "In your tool's webhook or integration settings, paste it as the callback URL.",
            "Set the method to POST with a JSON body. Name fields as name, mobile, email, query.",
            "Send a test submission. It shows up in Recent leads below.",
        ),
    ),
)

BY_KEY = {p.key: p for p in PLATFORMS}
