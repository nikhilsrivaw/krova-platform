"""
The few features a plan does not include. Every plan has every channel, the AI, automations and campaigns; only the
entry plan (Solo) leaves out the ones a one-person business does not need, so Starter has a reason to exist.

One table, read by the API (402 when a locked feature is asked for), by /auth/me (the sidebar shows a lock) and by
the background work that would otherwise run it (feedback rounds, the website chat). A business with no plan (a
trial, or one from before billing) has nothing locked - the same rule as shared/billing/limits.py.

Mirrored for the pricing page in krova-web/lib/planGuide.ts (NOT_ON_SOLO).
"""

from fastapi import HTTPException, status

WEBSITE_CHAT = "website_chat"
FEEDBACK = "feedback"
ANALYTICS = "analytics"
ASK_KROVA = "ask_krova"
TEAM_ROUTING = "team_routing"

LABELS = {
    WEBSITE_CHAT: "Website chat",
    FEEDBACK: "Feedback",
    ANALYTICS: "Analytics reports",
    ASK_KROVA: "Ask KROVA",
    TEAM_ROUTING: "Teams and routing rules",
}

LOCKED: dict[str, frozenset[str]] = {
    "solo": frozenset({WEBSITE_CHAT, FEEDBACK, ANALYTICS, ASK_KROVA, TEAM_ROUTING}),
}
# The plan that has it, named in the message.
_UNLOCKS_ON = "Starter"


def locked_for(plan: str | None) -> frozenset[str]:
    return LOCKED.get((plan or "").lower(), frozenset())


def has(business_or_plan, feature: str) -> bool:
    """Whether this business (or plan key) may use `feature`."""
    plan = business_or_plan if isinstance(business_or_plan, str) or business_or_plan is None else getattr(
        business_or_plan, "plan", None)
    return feature not in locked_for(plan)


def require(business, feature: str) -> None:
    """402 unless the business's plan includes `feature`."""
    if not has(business, feature):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "plan_feature",
                    "message": f"{LABELS[feature]} is not part of the Solo plan. Move to {_UNLOCKS_ON} on the Billing page."},
        )


def dependency(feature: str):
    """A FastAPI dependency that 402s a request for `feature` on a plan without it (shared by the module routers)."""
    from fastapi import Depends

    from services.api.dependencies import CurrentUserDep, DbDep

    async def check(current_user: CurrentUserDep, db: DbDep) -> None:
        from shared.db.models import Business

        if current_user.business is None:
            return
        require(await db.get(Business, current_user.business), feature)

    return Depends(check)
