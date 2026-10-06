"""
Receivables by file upload: the business exports its outstanding list from any
software as CSV, and this reconciles it into the Commitment Ledger.

Any row with a problem is reported by line number and skipped; the rest still
import. Nothing is written if the file itself cannot be read.
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import ImportRun
from shared.integrations.receivables import apply_receivables, parse_file

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
    name = (file.filename or "").lower()
    if not (name.endswith(".csv") or name.endswith(".xlsx")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload a .csv or .xlsx file")
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than 2 MB")

    try:
        rows, errors = parse_file(name, content)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if errors and not rows:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"message": "No rows could be imported", "errors": [e.__dict__ for e in errors]},
        )

    counts = await apply_receivables(db, current_user.business, SOURCE, rows, mark_missing_paid=mark_missing_paid)
    counts["skipped"] = len(errors)
    db.add(ImportRun(
        business_id=current_user.business,
        source=SOURCE,
        filename=file.filename,
        mark_missing_paid=mark_missing_paid,
        rows=len(rows),
        created=counts["created"],
        updated=counts["updated"],
        resolved=counts["resolved"],
        skipped=len(errors),
    ))
    await db.flush()
    counts["errors"] = [e.__dict__ for e in errors[:50]]
    return counts


@router.get("/history")
async def receivables_history(current_user: CurrentUserDep, db: DbDep, limit: int = 20) -> list[dict]:
    runs = await db.execute(
        select(ImportRun)
        .where(ImportRun.business_id == current_user.business)
        .order_by(ImportRun.created_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return [
        {
            "id": str(r.id),
            "source": r.source,
            "filename": r.filename,
            "mark_missing_paid": r.mark_missing_paid,
            "rows": r.rows,
            "created": r.created,
            "updated": r.updated,
            "resolved": r.resolved,
            "skipped": r.skipped,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in runs.scalars().all()
    ]
