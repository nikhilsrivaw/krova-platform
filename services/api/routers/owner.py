"""
"Ask KROVA": the owner asks a question in text, in the app, and gets an
answer from their own ledger. Same context the owner voice call uses
(agent_context.build_owner), but over text and a non-Anthropic model -
Claude is reserved for voice. Refused with a plain 503 until the chosen
provider is approved and its account quota is live (shared/ai/providers.py).
"""

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, status

from services.api.dependencies import CurrentUserDep, DbDep
from shared.ai import client, context as agent_context, providers
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/owner", tags=["owner"])

HISTORY_TURNS = 10

ANSWER_RULES = (
    "Answer in the same language the owner writes in (Hindi, Hinglish or English). "
    "Be brief: a few sentences. Use only the ledger data above. If the answer is "
    "not in it, say it is not tracked yet - never estimate."
)


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1, max_length=2000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=HISTORY_TURNS)


class AskOut(BaseModel):
    answer: str


@router.post("/ask", response_model=AskOut)
async def ask(body: AskIn, current_user: CurrentUserDep, db: DbDep) -> AskOut:
    owner = await agent_context.build_owner(current_user.business, db)
    system = f"{owner.render()}\n\n{ANSWER_RULES}"
    messages = [{"role": t.role, "content": t.text} for t in body.history[-HISTORY_TURNS:]]
    messages.append({"role": "user", "content": body.question})
    try:
        answer = await providers.call(
            settings.owner_ask_model,
            {"system": system, "messages": messages, "max_tokens": 600},
        )
    except providers.ProviderRefused as exc:
        logger.warning("owner ask refused: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Ask KROVA is not available yet. It needs its AI provider approved and running.",
        )
    client._log_usage("owner_ask", settings.owner_ask_model, answer.usage, answer.cost_paise)
    return AskOut(answer=answer.text.strip())
