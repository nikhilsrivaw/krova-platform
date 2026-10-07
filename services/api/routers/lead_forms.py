"""
Hosted public lead-intake forms - a business builds its own form (name,
phone, and any custom fields it wants), publishes it, and shares the link
anywhere (a bio, an ad, a QR code). A submission is parsed into the same
ParsedLead shape every other lead source uses and handed to
shared/leads/intake.py::ingest_parsed(), so it gets the same dedupe,
customer resolution, and lead.received automation as Justdial/IndiaMART/a
CSV import - the only new work here is building the form itself and
collecting a submission.

Two halves, one file - same shape as services/api/routers/kiosk.py:
- /forms/* (staff, JWT): build, publish, list, delete a business's own
  forms, and upload a form's logo.
- /forms/{token}/public, /forms/{token}/submit, /forms/{token}/upload
  (no auth): what the public page (krova-web's app/f/[token]/page.tsx)
  reads and posts to. Resolve-the-tenant-from-an-opaque-token, same auth
  model kiosk.py and widget.py already use for a public-but-not-just-
  anyone endpoint. An unknown or unpublished token is a 404, never a
  leakier "not published" error that would confirm a guessed token is at
  least real.

Two real-world gaps closed here, same reasoning shared/channels/web/
guardrails.py's own docstring gives for the chat widget: a honeypot
field catches a scripted bot without ever showing a real visitor a
CAPTCHA, and a public upload endpoint gets the same rate limit and a
tighter size/type check than the staff-authenticated Instagram carousel
upload (shared/integrations/media_storage.py) uses, since this one has
no login in front of it at all.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.channels.web import guardrails
from shared.config.settings import settings as app_settings
from shared.db.models import Business, InboundLead, LeadForm
from shared.integrations import media_storage
from shared.integrations.media_storage import MediaStorageError
from shared.leads import intake
from shared.leads.justdial_parse import ParsedLead
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/forms", tags=["forms"])

SOURCE = "form"
FIELD_TYPES = {"name", "phone", "email", "text", "textarea", "select", "checkbox", "file"}
MAX_FIELDS = 20

# Tighter than the chat widget's own 20/min (shared/channels/web/
# guardrails.py's default) - a lead form has no ongoing conversation to
# justify that volume from one visitor population, so the bar for "looks
# scripted" is lower here.
_FORM_RATE_WINDOW = timedelta(minutes=1)
_FORM_RATE_MAX = 10

_UPLOAD_ALLOWED_TYPES = {"image/jpeg", "image/png", "application/pdf"}
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_LOGO_ALLOWED_TYPES = {"image/jpeg", "image/png"}
_LOGO_MAX_BYTES = 5 * 1024 * 1024


class ShowIf(BaseModel):
    field_key: str
    equals: str


class FieldDef(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    type: str
    required: bool = False
    options: list[str] | None = None
    # Which page this field renders on - 0 is the first page. Fields are
    # grouped by this number, in the order they already have in the list.
    step: int = Field(default=0, ge=0, le=20)
    # Only rendered (and only enforced as required) once an earlier field
    # named field_key currently holds the value equals - null means always
    # shown. See _is_visible below for the one place this is evaluated.
    show_if: ShowIf | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in FIELD_TYPES:
            raise ValueError(f"Unknown field type {v!r} - must be one of {sorted(FIELD_TYPES)}")
        return v


def _validate_fields(fields: list[FieldDef]) -> None:
    if len(fields) > MAX_FIELDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A form can have at most {MAX_FIELDS} fields")
    keys = [f.key for f in fields]
    if len(keys) != len(set(keys)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Field keys must be unique within a form")
    by_key = {f.key: f for f in fields}
    for f in fields:
        if f.type == "select" and not f.options:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Field {f.label!r} is a dropdown but has no options")
        if f.show_if:
            if f.show_if.field_key == f.key:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Field {f.label!r} cannot depend on itself")
            target = by_key.get(f.show_if.field_key)
            if target is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Field {f.label!r} depends on an unknown field {f.show_if.field_key!r}",
                )
            if target.type not in ("select", "checkbox"):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Field {f.label!r} can only depend on a dropdown or checkbox field",
                )


def _is_visible(field: dict, values: dict[str, str]) -> bool:
    show_if = field.get("show_if")
    if not show_if:
        return True
    return (values.get(show_if["field_key"]) or "").strip() == show_if["equals"]


class FormIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    fields: list[FieldDef] = Field(default_factory=list)
    is_published: bool = False
    accent_color: str | None = Field(default=None, max_length=9)

    @field_validator("accent_color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.startswith("#") or len(v) not in (4, 7):
            raise ValueError("accent_color must be a hex color like #3B82F6")
        return v


class FormOut(BaseModel):
    id: str
    title: str
    description: str | None
    fields: list[FieldDef]
    is_published: bool
    public_url: str | None
    submission_count: int
    logo_url: str | None
    accent_color: str | None


async def _submission_count(db: DbDep, token: str) -> int:
    result = await db.execute(
        select(func.count(InboundLead.id)).where(
            InboundLead.source == SOURCE,
            InboundLead.raw_payload["form_token"].as_string() == token,
        )
    )
    return int(result.scalar_one())


def _public_url(token: str) -> str | None:
    if not app_settings.public_base_url:
        return None
    return f"{app_settings.public_base_url.rstrip('/')}/f/{token}"


async def _to_out(db: DbDep, form: LeadForm) -> FormOut:
    return FormOut(
        id=str(form.id), title=form.title, description=form.description,
        fields=[FieldDef(**f) for f in form.fields], is_published=form.is_published,
        public_url=_public_url(form.token) if form.is_published else None,
        submission_count=await _submission_count(db, form.token),
        logo_url=form.logo_url, accent_color=form.accent_color,
    )


async def _owned_form(form_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> LeadForm:
    form = await db.get(LeadForm, form_id)
    if form is None or form.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form not found")
    return form


@router.get("", response_model=list[FormOut])
async def list_forms(current_user: CurrentUserDep, db: DbDep) -> list[FormOut]:
    rows = await db.execute(
        select(LeadForm)
        .where(LeadForm.business_id == current_user.business)
        .order_by(LeadForm.created_at.desc())
    )
    return [await _to_out(db, f) for f in rows.scalars().all()]


@router.post("", response_model=FormOut, status_code=status.HTTP_201_CREATED)
async def create_form(body: FormIn, current_user: CurrentUserDep, db: DbDep) -> FormOut:
    _validate_fields(body.fields)
    form = LeadForm(
        business_id=current_user.business,
        title=body.title,
        description=body.description,
        token=intake.new_token()[:24],
        fields=[f.model_dump() for f in body.fields],
        is_published=body.is_published,
        accent_color=body.accent_color,
    )
    db.add(form)
    await db.flush()
    return await _to_out(db, form)


@router.patch("/{form_id}", response_model=FormOut)
async def update_form(form_id: uuid.UUID, body: FormIn, current_user: CurrentUserDep, db: DbDep) -> FormOut:
    form = await _owned_form(form_id, current_user, db)
    _validate_fields(body.fields)
    form.title = body.title
    form.description = body.description
    form.fields = [f.model_dump() for f in body.fields]
    form.is_published = body.is_published
    form.accent_color = body.accent_color
    await db.flush()
    return await _to_out(db, form)


@router.delete("/{form_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_form(form_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    form = await _owned_form(form_id, current_user, db)
    # Past submissions (inbound_leads rows) are kept - they are independent
    # rows, not foreign-keyed to this form, same as every other lead source.
    await db.delete(form)
    await db.flush()


@router.post("/{form_id}/logo", response_model=FormOut)
async def upload_form_logo(
    form_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep, file: UploadFile = File(...),
) -> FormOut:
    form = await _owned_form(form_id, current_user, db)
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    try:
        logo_url = await media_storage.upload_media(
            content, content_type, key_prefix="lead-forms/logos",
            max_bytes=_LOGO_MAX_BYTES, allowed_content_types=_LOGO_ALLOWED_TYPES,
        )
    except MediaStorageError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    form.logo_url = logo_url
    await db.flush()
    return await _to_out(db, form)


# ── Public: the hosted page itself ────────────────────────────────────────


async def _published_form(token: str, db: DbDep) -> LeadForm:
    result = await db.execute(
        select(LeadForm).where(LeadForm.token == token, LeadForm.is_published.is_(True))
    )
    form = result.scalars().first()
    if form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form not found")
    return form


class PublicFormOut(BaseModel):
    title: str
    description: str | None
    fields: list[FieldDef]
    logo_url: str | None
    accent_color: str | None


@router.get("/{token}/public", response_model=PublicFormOut)
async def get_public_form(token: str, db: DbDep) -> PublicFormOut:
    form = await _published_form(token, db)
    return PublicFormOut(
        title=form.title, description=form.description, fields=[FieldDef(**f) for f in form.fields],
        logo_url=form.logo_url, accent_color=form.accent_color,
    )


class UploadOut(BaseModel):
    url: str


@router.post("/{token}/upload", response_model=UploadOut)
async def upload_form_file(token: str, db: DbDep, file: UploadFile = File(...)) -> UploadOut:
    """A visitor attaching a file to a "file" field, before the rest of the
    form is submitted - two steps (upload, then submit carrying the
    returned URL as that field's value) rather than one multipart submit,
    so the builder/public page can show an upload-in-progress state per
    field without blocking the whole form."""
    form = await _published_form(token, db)
    if not guardrails.check_rate_limit(form, window=_FORM_RATE_WINDOW, max_requests=_FORM_RATE_MAX):
        await db.flush()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads - please try again shortly")
    await db.flush()

    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    try:
        url = await media_storage.upload_media(
            content, content_type, key_prefix="lead-forms/uploads",
            max_bytes=_UPLOAD_MAX_BYTES, allowed_content_types=_UPLOAD_ALLOWED_TYPES,
        )
    except MediaStorageError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return UploadOut(url=url)


class FormSubmitIn(BaseModel):
    values: dict[str, str]
    # A hidden field real visitors never see or fill (the public page keeps
    # it visually off-screen) - a non-empty value here means something
    # filled in every input on the page, which a human reading the form
    # never would. Caught silently: the caller gets a normal-looking
    # success so a bot never learns it was detected.
    hp: str = ""


def _build_parsed_lead(fields: list[dict], values: dict[str, str]) -> tuple[ParsedLead, dict]:
    """Maps a submission onto the shared ParsedLead shape. Pure/DB-free so
    it's testable on its own, same reasoning shared/leads/justdial_parse.py
    gives for keeping its own parser free of database imports. Fields
    hidden by show_if are skipped entirely, not just unenforced - a value
    left behind in the browser from a since-changed earlier answer should
    not show up in the lead."""
    name = phone = email = None
    query_parts: list[str] = []
    stored: dict = {}

    for f in fields:
        if not _is_visible(f, values):
            continue
        value = (values.get(f["key"]) or "").strip()
        stored[f["key"]] = value
        if not value:
            continue
        if f["type"] == "name" and name is None:
            name = value
        elif f["type"] == "phone" and phone is None:
            phone = value
        elif f["type"] == "email" and email is None:
            email = value
        else:
            query_parts.append(f"{f['label']}: {value}")

    parsed = ParsedLead(name=name, phone=phone, email=email, query="; ".join(query_parts) or None, external_id=None)
    return parsed, stored


@router.post("/{token}/submit")
async def submit_form(token: str, body: FormSubmitIn, db: DbDep) -> dict:
    form = await _published_form(token, db)

    if body.hp.strip():
        logger.info("form submission honeypot tripped form=%s", form.id)
        return {"status": "received"}

    if not guardrails.check_rate_limit(form, window=_FORM_RATE_WINDOW, max_requests=_FORM_RATE_MAX):
        await db.flush()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many submissions - please try again shortly")

    missing = [
        f["label"] for f in form.fields
        if f.get("required") and _is_visible(f, body.values) and not body.values.get(f["key"], "").strip()
    ]
    if missing:
        await db.flush()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Missing required field(s): {', '.join(missing)}")

    business = await db.get(Business, form.business_id)
    if business is None or not business.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form not found")

    parsed, stored = _build_parsed_lead(form.fields, body.values)
    payload = {**stored, "form_token": token, "form_title": form.title}
    row = await intake.ingest_parsed(db, business, SOURCE, parsed, payload)
    logger.info("form submission form=%s business=%s status=%s", form.id, business.id, row.status)
    return {"status": row.status}
