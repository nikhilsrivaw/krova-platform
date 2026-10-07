"""
Leads that arrive by hand: a file export from a portal (CSV or Excel), or one
lead typed in. Each row goes through the same intake as webhook leads, so
dedupe, customer linking, and the enquiry note all behave the same way.

Source is chosen by the business (the portal the leads came from), so the
ledger still shows where each lead originated.
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Business
from shared.leads import intake
from shared.leads.justdial_parse import parse_lead
from shared.leads.lead_file import parse_lead_file

router = APIRouter(prefix="/leads", tags=["leads"])

ALLOWED_SOURCES = {"magicbricks", "99acres", "housing", "justdial", "other"}
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000


class ManualLeadIn(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    query: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="other")


def _check_source(source: str) -> str:
    if source not in ALLOWED_SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown lead source '{source}'")
    return source


@router.post("/import")
async def import_leads(
    current_user: CurrentUserDep,
    db: DbDep,
    file: UploadFile = File(...),
    source: str = Form(...),
) -> dict:
    source = _check_source(source)
    name = (file.filename or "").lower()
    if not (name.endswith(".csv") or name.endswith(".xlsx")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload a .csv or .xlsx file")
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than 2 MB")

    try:
        records = parse_lead_file(name, content)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not records:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No lead rows found in the file")
    if len(records) > MAX_ROWS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"More than {MAX_ROWS} rows. Split the file.")

    parsed = [parse_lead(record) for record in records]
    if not any(lead.name or lead.phone or lead.email for lead in parsed):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not find a name, phone, or email column in this file. Check the column headers.",
        )

    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    counts = {"received": 0, "duplicate": 0, "no_phone": 0}
    for record, lead in zip(records, parsed):
        row = await intake.ingest_parsed(db, business, source, lead, record)
        counts[row.status] = counts.get(row.status, 0) + 1
    return {"source": source, "rows": len(records), **counts}


@router.post("/manual")
async def add_lead_manually(body: ManualLeadIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    source = _check_source(body.source)
    if not (body.phone or body.email or body.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter at least a name, phone, or email")
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    record = {"name": body.name, "mobile": body.phone, "email": body.email, "query": body.query}
    row = await intake.ingest_parsed(db, business, source, parse_lead(record), record)
    return {"status": row.status, "id": str(row.id)}
