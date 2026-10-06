"""
Receivables by file upload: the business exports its outstanding list from any
software as CSV, and this reconciles it into the Commitment Ledger.

Any row with a problem is reported by line number and skipped; the rest still
import. Nothing is written if the file itself cannot be read.
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from services.api.dependencies import CurrentUserDep, DbDep
from shared.integrations.receivables import apply_receivables, parse_csv

router = APIRouter(prefix="/receivables", tags=["receivables"])

MAX_BYTES = 2 * 1024 * 1024
SOURCE = "csv"


@router.post("/import")
async def import_receivables(
    current_user: CurrentUserDep,
    db: DbDep,
    file: UploadFile = File(...),
    mark_missing_paid: bool = Form(default=True),
) -> dict:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload a .csv file")
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than 2 MB")

    rows, errors = parse_csv(content)
    if errors and not rows:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"message": "No rows could be imported", "errors": [e.__dict__ for e in errors]},
        )

    counts = await apply_receivables(db, current_user.business, SOURCE, rows, mark_missing_paid=mark_missing_paid)
    counts["skipped"] = len(errors)
    counts["errors"] = [e.__dict__ for e in errors[:50]]
    return counts
