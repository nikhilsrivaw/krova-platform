"""
Managing WhatsApp Flows via the Graph API.

Per Meta's Flows API reference, authoring one is three calls:

  1. POST /{WABA-ID}/flows           - create the (empty) flow, get an id
  2. POST /{FLOW-ID}/assets          - upload the Flow JSON that defines it
  3. POST /{FLOW-ID}/publish         - make it sendable

Step 2 is the one worth reading closely. Meta's asset upload is a
multipart/form-data POST, not a JSON body - the Flow JSON goes in as a file
part named "file", alongside form fields "name" and "asset_type". Getting
this wrong (posting the JSON as a body, or as the wrong field name) fails
with an opaque "invalid parameter" rather than anything pointing at the
actual mistake.

Deliberately scoped to navigate flows only - see shared/db/models/flow.py's
module docstring for why. Nothing here handles a data_exchange endpoint or
the RSA/AES key exchange it would require.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class FlowError(Exception):
    """A Flow operation Meta refused. The message is shown to the business."""


@dataclass(slots=True)
class FlowValidationIssue:
    error_type: str | None
    message: str | None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(slots=True)
class CreatedFlow:
    flow_id: str
    validation_errors: list[FlowValidationIssue] = field(default_factory=list)


def _explain(response: httpx.Response) -> FlowError:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    error = (payload or {}).get("error") or {}
    detail = error.get("error_user_msg") or error.get("message") or response.text[:300]
    return FlowError(detail or "Meta rejected the Flow request")


def _issues(raw: list[dict] | None) -> list[FlowValidationIssue]:
    return [
        FlowValidationIssue(
            error_type=item.get("error_type"),
            message=item.get("message"),
            line_start=item.get("line_start"),
            line_end=item.get("line_end"),
        )
        for item in (raw or [])
    ]


async def create_flow(
    access_token: str, waba_id: str, name: str, categories: list[str]
) -> str:
    """Create an empty flow shell and return its id. No screens yet."""
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            f"{settings.graph_base_url}/{waba_id}/flows",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"name": name, "categories": categories},
        )
    if response.status_code != 200:
        logger.warning("flow create failed waba=%s: %s", waba_id, response.text[:300])
        raise _explain(response)
    flow_id = response.json().get("id")
    if not flow_id:
        raise FlowError("Meta did not return a flow id")
    return flow_id


async def upload_flow_json(
    access_token: str, flow_id: str, flow_json: dict[str, Any]
) -> list[FlowValidationIssue]:
    """
    Push the Flow JSON that defines this flow's screens.

    Returns whatever validation issues Meta found - callers decide whether
    those block publishing. An empty list means Meta accepted it clean.
    """
    import json as _json

    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            f"{settings.graph_base_url}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
            files={"file": ("flow.json", _json.dumps(flow_json), "application/json")},
        )
    if response.status_code != 200:
        logger.warning("flow json upload failed flow=%s: %s", flow_id, response.text[:300])
        raise _explain(response)
    return _issues(response.json().get("validation_errors"))


async def publish_flow(access_token: str, flow_id: str) -> None:
    """
    Make a flow sendable. Meta refuses this if validation errors remain -
    the failure message is Meta's own, surfaced rather than reworded, since
    it names the exact screen/component at fault.
    """
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            f"{settings.graph_base_url}/{flow_id}/publish",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200 or not response.json().get("success"):
        logger.warning("flow publish failed flow=%s: %s", flow_id, response.text[:300])
        raise _explain(response)


async def get_flow_status(access_token: str, flow_id: str) -> dict:
    """Read a flow's current status and validation state straight from Meta."""
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.get(
            f"{settings.graph_base_url}/{flow_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,status,categories,validation_errors,json_version"},
        )
    if response.status_code != 200:
        raise _explain(response)
    return response.json()


async def deprecate_flow(access_token: str, flow_id: str) -> None:
    """
    Retire a published flow. Meta does not allow deleting a published one -
    deprecation is the only way to stop it being sendable again.
    """
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            f"{settings.graph_base_url}/{flow_id}/deprecate",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200:
        raise _explain(response)
