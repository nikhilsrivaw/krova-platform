"""
Configuration for the Krova platform.

One settings object for every service - API, workers and voice - so a value
means the same thing everywhere. Anything secret comes from the environment;
nothing secret has a default.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Runtime ──────────────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = Field(
        default="development", alias="ENVIRONMENT"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")

    # Public origin this service is reachable at. Used for webhook callbacks
    # and OAuth redirects, so it must match what Meta and Google have on file.
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(alias="DATABASE_URL")

    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        # SQLAlchemy needs the driver named explicitly; a plain postgresql://
        # URL silently selects psycopg2 and then fails on the first await.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # ── Auth ─────────────────────────────────────────────────────────────────
    # We issue our own tokens. No external identity provider.
    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_ttl_minutes: int = Field(default=30, alias="ACCESS_TOKEN_TTL_MINUTES")
    refresh_token_ttl_days: int = Field(default=30, alias="REFRESH_TOKEN_TTL_DAYS")

    # Encrypts channel credentials at rest. Fernet key, 44 chars base64.
    encryption_key: str = Field(alias="ENCRYPTION_KEY")

    # ── Meta (WhatsApp + Instagram) ──────────────────────────────────────────
    meta_app_id: str = Field(default="", alias="META_APP_ID")
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_webhook_verify_token: str = Field(default="", alias="META_WEBHOOK_VERIFY_TOKEN")
    # Embedded Signup requires v25.0 or later.
    meta_api_version: str = Field(default="v25.0", alias="META_API_VERSION")
    # Facebook Login for Business -> Configurations. WhatsApp's Embedded
    # Signup configuration - its own permission set, distinct from the
    # Instagram one below.
    meta_config_id: str = Field(default="", alias="META_CONFIG_ID")
    # A separate Facebook Login for Business Configuration for Instagram
    # messaging - instagram_basic/instagram_manage_messages/pages_show_list/
    # etc. Facebook Login for Business does not accept a raw scope= param;
    # the permission set has to be pre-built as a Configuration in the
    # dashboard and referenced by this id instead, or Meta rejects the
    # requested scopes as invalid even when they're spelled correctly.
    instagram_fb_config_id: str = Field(default="", alias="INSTAGRAM_FB_CONFIG_ID")

    # "Instagram API with Instagram Login" issues its own separate app
    # identity - its own id and secret, distinct from meta_app_id/secret
    # above. A webhook for Instagram messaging/comments is signed with
    # THIS secret, not the main app's - conflating the two would make every
    # Instagram webhook signature check fail.
    meta_instagram_app_id: str = Field(default="", alias="META_INSTAGRAM_APP_ID")
    meta_instagram_app_secret: str = Field(default="", alias="META_INSTAGRAM_APP_SECRET")
    # Must match, character for character, what is registered in Meta's
    # dashboard as this app's OAuth Redirect URI - the token exchange call
    # is rejected otherwise, and the two are configured completely separately.
    instagram_redirect_uri: str = Field(default="", alias="INSTAGRAM_REDIRECT_URI")
    # Separate from the one above - this flow goes through the main app's
    # Facebook Login for Business, a different OAuth dialog entirely, and
    # Meta requires its own registered redirect URI matching exactly.
    instagram_fb_redirect_uri: str = Field(default="", alias="INSTAGRAM_FB_REDIRECT_URI")

    # Where a browser lands after the Instagram Business Login round trip -
    # a page under the actual dashboard, not the API itself, since the API
    # has nothing to show a human.
    frontend_base_url: str = Field(default="https://krova.space", alias="FRONTEND_BASE_URL")

    # India data residency for a connected number. Kept because we want it,
    # but NOT currently applied: passing data_localization_region to
    # /register is rejected on v21.0+, and local storage can now only be
    # changed while a number is unregistered. Revisit when onboarding a
    # client for whom residency is contractual.
    whatsapp_data_localization_region: str = Field(
        default="IN", alias="WHATSAPP_DATA_LOCALIZATION_REGION"
    )

    # ── Anthropic ────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Anything a caller waits on. Measured ~0.78s to first token.
    claude_fast_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="CLAUDE_FAST_MODEL"
    )
    # Overnight analysis, where quality matters more than latency.
    claude_deep_model: str = Field(
        default="claude-sonnet-5", alias="CLAUDE_DEEP_MODEL"
    )

    # ── Google / Microsoft (email channels) ──────────────────────────────────
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    microsoft_client_id: str = Field(default="", alias="MICROSOFT_CLIENT_ID")
    microsoft_client_secret: str = Field(default="", alias="MICROSOFT_CLIENT_SECRET")

    # ── Voice (added when the voice service ships) ───────────────────────────
    plivo_auth_id: str = Field(default="", alias="PLIVO_AUTH_ID")
    plivo_auth_token: str = Field(default="", alias="PLIVO_AUTH_TOKEN")
    sarvam_api_key: str = Field(default="", alias="SARVAM_API_KEY")

    # Comma-separated. Empty in production by default was the bug this
    # replaced - it silently blocked every browser request to the API with
    # no origin allowed at all, discovered only once something was actually
    # deployed for the first time.
    cors_allowed_origins_raw: str = Field(
        default="https://krova.space,https://www.krova.space", alias="CORS_ALLOWED_ORIGINS"
    )

    @property
    def cors_allowed_origins(self) -> list[str]:
        if self.is_development:
            return ["http://localhost:3000"]
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]

    # ── Limits ───────────────────────────────────────────────────────────────
    api_rate_limit_per_minute: int = Field(default=120, alias="API_RATE_LIMIT_PER_MINUTE")
    webhook_rate_limit_per_minute: int = Field(
        default=1000, alias="WEBHOOK_RATE_LIMIT_PER_MINUTE"
    )

    # ── Platform operator ─────────────────────────────────────────────────────
    # Gates the handful of cross-business admin endpoints (140/160-series
    # number request queue) that exist for Krova's own operator, not any
    # business. A single email check rather than a new cross-tenant role
    # system - appropriate while there is exactly one such operator and
    # this is used at low volume. Empty by default, which is what makes
    # every admin-gated endpoint refuse everyone until this is set.
    platform_admin_email: str = Field(default="", alias="PLATFORM_ADMIN_EMAIL")

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}"

    @property
    def instagram_graph_base_url(self) -> str:
        # A different host from graph_base_url above, not a typo - "Instagram
        # API with Instagram Login" runs on its own host, separate from the
        # main Graph API WhatsApp uses.
        return f"https://graph.instagram.com/{self.meta_api_version}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
