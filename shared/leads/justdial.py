"""
Justdial lead intake. The field mapping lives in justdial_parse; the shared
intake (dedupe, customer link, note) lives in intake.

Dedupe is by Justdial's own lead id, when the payload has one. Without an id
the same lead sent twice would be stored twice, and that is the known gap.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, InboundLead
from shared.leads import intake
from shared.leads.justdial_parse import parse_lead

SOURCE = "justdial"
TOKEN_SETTING = "justdial_token_hash"

new_token = intake.new_token
hash_token = intake.hash_token


async def ingest(db: AsyncSession, business: Business, payload: dict) -> InboundLead:
    return await intake.ingest_parsed(db, business, SOURCE, parse_lead(payload), payload)
