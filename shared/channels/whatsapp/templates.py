"""
Creating and managing templates on a client's WhatsApp Business Account.

Meta's Message Templates API, wrapped so the rest of the platform never has to
know its shape.

The parts that are easy to get wrong, and are therefore handled here:

Every variable needs an example. Meta rejects a template whose {{placeholder}}
has no sample value, because a human reviewer reads the example to judge what
the template is for. Callers give us the variable names; we build the example
block.

Names are constrained. Lowercase letters, digits and underscores only - a
client typing "Payment Reminder" would be refused by Meta with an unhelpful
error, so it is normalised before it ever leaves us.

Approval is asynchronous. Creating returns PENDING and nothing more; the real
answer arrives on the message_template_status_update webhook up to 24 hours
later. Nothing here waits for it.

Deleting by name removes every language variant. That is Meta's behaviour, not
a choice, and it deserves a confirmation in any UI that exposes it.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

Category = Literal["UTILITY", "MARKETING", "AUTHENTICATION"]

# Meta's ceilings. Enforced here so a client sees a useful message rather than
# a rejected submission a day later.
MAX_NAME = 512
MAX_BODY = 1024
MAX_HEADER_TEXT = 60
MAX_FOOTER = 60
MAX_BUTTONS = 10

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9_]+")


class TemplateError(Exception):
    """Meta refused a template operation. The message is shown to the client."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


# Meta's error codes worth translating into something a business owner can act on.
_MEANINGS = {
    132000: "The number of variables does not match the example values",
    132001: "A template with that name and language already exists",
    132005: "This template was edited after approval and must be reviewed again",
    132007: "The template content breaks WhatsApp's content policy",
    132012: "A variable's example value is missing or badly formatted",
    132015: "This template is paused and cannot be used until it is edited",
    132016: "This template is disabled because of customer feedback",
    132068: "Template flow is paused",
    132069: "Template flow is unpublished",
    100: "Meta rejected the request - check the template name and content",
}


def normalise_name(raw: str) -> str:
    """
    Turn what a person typed into a name Meta will accept.

    "Payment Reminder!" -> "payment_reminder". Done for the client rather than
    refusing them, because the constraint is Meta's and is not interesting to
    the person writing a reminder.
    """
    name = _INVALID_NAME_CHARS.sub("_", raw.strip().lower()).strip("_")
    name = re.sub(r"_{2,}", "_", name)
    if not name:
        raise TemplateError("Give the template a name")
    return name[:MAX_NAME]


def parameter_format(variables: list[str]) -> str:
    """
    Which placeholder style these variables use.

    Meta defaults to POSITIONAL, so a named template must say so or be
    rejected with INVALID_FORMAT. All-numeric variables are positional
    ({{1}}, {{2}}); anything else is named.
    """
    if variables and all(v.isdigit() for v in variables):
        return "POSITIONAL"
    return "NAMED"


def variables_in(text: str) -> list[str]:
    """The {{placeholders}} in a piece of text, in order, without duplicates."""
    seen: list[str] = []
    for match in _VARIABLE.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


@dataclass(slots=True)
class Button:
    type: Literal["QUICK_REPLY", "URL", "PHONE_NUMBER"]
    text: str
    url: str | None = None
    phone_number: str | None = None


@dataclass(slots=True)
class TemplateDraft:
    """What a client wrote, before it becomes Meta's JSON."""

    name: str
    category: Category
    body: str
    language: str = "en"
    header_text: str | None = None
    footer: str | None = None
    buttons: list[Button] = field(default_factory=list)
    # Sample values per variable name. Missing ones get a placeholder, because
    # Meta refuses the template outright without them.
    examples: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.body or not self.body.strip():
            raise TemplateError("The message body cannot be empty")
        if len(self.body) > MAX_BODY:
            raise TemplateError(f"The body must be under {MAX_BODY} characters")
        if self.header_text and len(self.header_text) > MAX_HEADER_TEXT:
            raise TemplateError(f"The header must be under {MAX_HEADER_TEXT} characters")
        if self.footer and len(self.footer) > MAX_FOOTER:
            raise TemplateError(f"The footer must be under {MAX_FOOTER} characters")
        if len(self.buttons) > MAX_BUTTONS:
            raise TemplateError(f"A template can have at most {MAX_BUTTONS} buttons")
        if len(variables_in(self.header_text or "")) > 1:
            raise TemplateError("A header can contain at most one variable")

    def to_components(self) -> list[dict[str, Any]]:
        """
        Build Meta's components array.

        Named parameters throughout: {{customer_name}} reads far better in a
        template a human is editing than {{1}}, and Meta supports both.
        """
        self.validate()
        components: list[dict[str, Any]] = []

        if self.header_text:
            header: dict[str, Any] = {
                "type": "HEADER",
                "format": "TEXT",
                "text": self.header_text,
            }
            header_vars = variables_in(self.header_text)
            if header_vars:
                if parameter_format(header_vars) == "POSITIONAL":
                    header["example"] = {
                        "header_text": [self._sample(v) for v in header_vars]
                    }
                else:
                    header["example"] = {
                        "header_text_named_params": [
                            {"param_name": v, "example": self._sample(v)}
                            for v in header_vars
                        ]
                    }
            components.append(header)

        body: dict[str, Any] = {"type": "BODY", "text": self.body}
        body_vars = variables_in(self.body)
        if body_vars:
            if parameter_format(body_vars) == "POSITIONAL":
                # Positional examples are a list of lists - one row of sample
                # values, not one object per variable.
                body["example"] = {
                    "body_text": [[self._sample(v) for v in body_vars]]
                }
            else:
                body["example"] = {
                    "body_text_named_params": [
                        {"param_name": v, "example": self._sample(v)}
                        for v in body_vars
                    ]
                }
        components.append(body)

        if self.footer:
            components.append({"type": "FOOTER", "text": self.footer})

        if self.buttons:
            components.append(
                {"type": "BUTTONS", "buttons": [self._button(b) for b in self.buttons]}
            )

        return components

    def _sample(self, variable: str) -> str:
        # A variable with no example is an instant rejection, so never send an
        # empty one - a readable placeholder is better than a refused template.
        supplied = (self.examples.get(variable) or "").strip()
        return supplied or variable.replace("_", " ").title()

    @staticmethod
    def _button(b: Button) -> dict[str, Any]:
        out: dict[str, Any] = {"type": b.type, "text": b.text}
        if b.type == "URL":
            if not b.url:
                raise TemplateError(f"Button '{b.text}' needs a URL")
            out["url"] = b.url
        elif b.type == "PHONE_NUMBER":
            if not b.phone_number:
                raise TemplateError(f"Button '{b.text}' needs a phone number")
            out["phone_number"] = b.phone_number
        return out


def _explain(payload: dict) -> TemplateError:
    error = payload.get("error") or {}
    code = error.get("code")
    detail = (
        error.get("error_user_msg")
        or (error.get("error_data") or {}).get("details")
        or error.get("message")
        or ""
    )
    known = _MEANINGS.get(code)
    message = known or detail or "Meta rejected the template"
    if known and detail and detail not in known:
        message = f"{known} ({detail})"
    return TemplateError(message, code=code)


class TemplateClient:
    """Template operations against one client's WABA."""

    def __init__(self, access_token: str, waba_id: str, *, timeout: float = 25.0):
        self._token = access_token
        self._waba_id = waba_id
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{settings.graph_base_url}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, headers=self._headers, **kwargs)
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            error = _explain(payload)
            logger.warning(
                "template %s %s failed (%s): %s", method, path, error.code, error
            )
            raise error
        return payload

    async def create(self, draft: TemplateDraft) -> dict:
        """
        Submit a template for review.

        Returns Meta's id and status. That status is PENDING - approval takes
        up to 24 hours and arrives on the webhook, not here.
        """
        components = draft.to_components()
        body = {
            "name": normalise_name(draft.name),
            "language": draft.language,
            "category": draft.category,
            "components": components,
            # Without this Meta assumes POSITIONAL and rejects named
            # placeholders with INVALID_FORMAT.
            "parameter_format": parameter_format(variables_in(draft.body)),
        }
        result = await self._call("POST", f"{self._waba_id}/message_templates", json=body)
        logger.info(
            "template submitted waba=%s name=%s id=%s",
            self._waba_id,
            body["name"],
            result.get("id"),
        )
        return result

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {
            "fields": "id,name,language,category,status,components,"
            "rejected_reason,quality_score",
            "limit": min(limit, 250),
        }
        if status:
            params["status"] = status
        payload = await self._call(
            "GET", f"{self._waba_id}/message_templates", params=params
        )
        return payload.get("data", [])

    async def edit(
        self,
        template_id: str,
        draft: TemplateDraft,
        *,
        category: Category | None = None,
    ) -> dict:
        """
        Replace a template's content.

        Meta replaces every component - there is no partial edit - and only
        APPROVED, REJECTED or PAUSED templates may be edited at all. Approved
        ones are capped at 10 edits per 30 days.
        """
        body: dict[str, Any] = {
            "components": draft.to_components(),
            "parameter_format": parameter_format(variables_in(draft.body)),
        }
        if category:
            body["category"] = category
        return await self._call("POST", template_id, json=body)

    async def delete(self, name: str, *, template_id: str | None = None) -> bool:
        """
        Delete a template.

        Without an id this removes EVERY language variant sharing the name -
        Meta's behaviour, and worth confirming with the client first.
        """
        params: dict[str, Any] = {"name": name}
        if template_id:
            params["hsm_id"] = template_id
        payload = await self._call(
            "DELETE", f"{self._waba_id}/message_templates", params=params
        )
        return bool(payload.get("success", True))
