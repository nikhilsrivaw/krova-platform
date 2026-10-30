"""
"Ask KROVA": the owner asks anything about their own business in text, in the app - money, customers, bookings,
orders, leads, calls, the team - and gets an answer from KROVA's own records (shared/ai/owner_ask.py), on a
non-Anthropic model - Claude is reserved for voice. Refused with a plain 503 until the chosen
provider is approved and its account quota is live (shared/ai/providers.py).
"""

from datetime import datetime

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, status

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.ai import client, providers
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/owner", tags=["owner"])

HISTORY_TURNS = 10


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1, max_length=2000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=HISTORY_TURNS)


class AskOut(BaseModel):
    answer: str


@router.post("/ask", response_model=AskOut)
async def ask(body: AskIn, current_user: OwnerOrAdminDep, db: DbDep) -> AskOut:
    """
    Two steps (shared/ai/owner_ask.py): the model first says which period, customers and parts of the business the
    question needs; those records are gathered here; then it answers from them alone.
    """
    from shared.ai import owner_ask
    from shared.db.models import Business

    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    today = datetime.now(owner_ask._tz(business)).date()
    history = [{"role": t.role, "content": t.text} for t in body.history[-HISTORY_TURNS:]]
    question = [*history, {"role": "user", "content": body.question}]
    try:
        planned = await providers.call(settings.owner_ask_model, {
            "system": owner_ask.plan_prompt(today), "messages": question, "max_tokens": 300,
            "tools": [owner_ask.PLAN_TOOL], "tool_choice": {"type": "tool", "name": owner_ask.PLAN_TOOL["name"]},
        })
        client._log_usage("owner_ask", settings.owner_ask_model, planned.usage, planned.cost_paise)
        plan = owner_ask.read_plan(planned.tool_input, today)
        facts = await owner_ask.gather(db, business, plan)
        answer = await providers.call(
            settings.owner_ask_model,
            {"system": owner_ask.answer_system(facts), "messages": question, "max_tokens": 900},
        )
    except providers.ProviderRefused as exc:
        logger.warning("owner ask refused: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Ask KROVA is not available yet. It needs its AI provider approved and running.",
        )
    client._log_usage("owner_ask", settings.owner_ask_model, answer.usage, answer.cost_paise)
    logger.info("owner ask business=%s plan=%s facts_chars=%s", business.id, owner_ask.jsonable(plan), len(facts))
    return AskOut(answer=answer.text.strip())
