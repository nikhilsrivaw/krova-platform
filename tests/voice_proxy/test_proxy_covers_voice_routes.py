import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi.routing import APIRoute  # noqa: E402


def _voice_http_paths(app) -> set[str]:
    return {r.path for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/voice")}


def test_every_voice_service_route_is_forwarded_by_the_api_proxy():
    """
    Plivo only ever talks to the API's public domain; services/api/
    voice_proxy.py has to forward each /voice/* path to the voice process
    explicitly. A route added to the voice service but not to the proxy
    404s in production while working perfectly when the voice service is
    hit directly - which has now happened twice (adhoc-answer, then the
    login OTP call's otp-answer), each time silently, on a live call.
    """
    from services.api.main import app as api_app
    from services.voice.main import app as voice_app

    missing = _voice_http_paths(voice_app) - _voice_http_paths(api_app)
    assert not missing, (
        f"voice service routes with no forward in services/api/voice_proxy.py: {sorted(missing)}"
    )
