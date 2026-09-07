"""
Feature-request deduplication - product_feedback capability.

Research named this "the single most impactful capability" for a
feedback tool: when the same request shows up worded five different
ways, an unmerged list makes a brand's single most-wanted feature look
like five mediocre ones. Unlike the deterministic joins everywhere else
in shared/care/, this needs an LLM call - "add dark mode" and "make it
easier to use at night" are the same request, and no string match will
ever see that.

Provenance still holds even though the matching isn't deterministic: a
merge never invents a headcount, it appends the duplicate's own real
source_message_ids onto the canonical Insight's array. "12 people want
this" is always backed by 12 real conversations a founder can open, the
same rule every other derived fact in this schema already follows.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.ai import client
from shared.db.models import Business, Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MATCH_TOOL = {
    "name": "match_request",
    "description": (
        "Decide whether a new feature request is the same underlying ask as "
        "one already on the list, even if worded completely differently. "
        "Return null if it is genuinely a different request, or a new kind "
        "of ask nothing on the list covers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_index": {
                "type": ["integer", "null"],
                "description": "The index (from the numbered list given) of the existing request this duplicates, or null if it's not a duplicate of any of them.",
            },
        },
        "required": ["matched_index"],
    },
}

SYSTEM = """You compare one new feature request against a numbered list of \
already-open feature requests for the same product, and decide whether it's \
the same underlying ask.

Match on the underlying need, not the wording - "add dark mode" and "make \
it easier to use at night" are the same request. Do not match on topic \
alone - "export to PDF" and "export to CSV" are different requests even \
though both are about exporting.

When genuinely unsure, prefer null (not a duplicate) - a missed merge just \
means two list entries instead of one; a wrong merge hides a real, distinct \
request inside another one's evidence."""


async def _match(new_title: str, new_body: str | None, candidates: list[Insight]) -> int | None:
    if not candidates:
        return None

    listing = "\n".join(f"{i}. {c.title}" + (f" - {c.body}" if c.body else "") for i, c in enumerate(candidates))
    prompt = (
        f"New request: {new_title}" + (f" - {new_body}" if new_body else "") + "\n\n"
        f"Already-open requests:\n{listing}\n\n"
        "Which one (if any) does the new request duplicate?"
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        tool=MATCH_TOOL,
        max_tokens=256,
    )
    matched = (completion.tool_input or {}).get("matched_index")
    if not isinstance(matched, int) or not (0 <= matched < len(candidates)):
        return None
    return matched


async def check_feature_request_dedup(db: AsyncSession) -> int:
    """Merge newly-created feature_request Insights into an existing open
    one when they're the same underlying ask. Returns how many were merged."""
    now = datetime.now(timezone.utc)

    result = await db.execute(select(Business).where(Business.is_active.is_(True)))
    businesses = [b for b in result.scalars().all() if verticals.has_capability(b.vertical, "product_feedback")]
    if not businesses:
        return 0

    merged = 0
    for business in businesses:
        open_rows = (
            await db.execute(
                select(Insight).where(
                    Insight.business_id == business.id,
                    Insight.kind == "feature_request",
                    Insight.dismissed_at.is_(None),
                ).order_by(Insight.created_at.asc())
            )
        ).scalars().all()

        unchecked = [r for r in open_rows if r.dedup_checked_at is None]
        if not unchecked:
            continue

        # Candidates start as every already-checked open row, and grow as
        # this run processes unchecked rows that turn out not to match
        # anything - so two duplicates created in the same sweep still
        # merge with each other, not just against pre-existing ones.
        candidates = [r for r in open_rows if r.dedup_checked_at is not None]

        for new_row in unchecked:
            match_idx = await _match(new_row.title, new_row.body, candidates)
            if match_idx is None:
                new_row.dedup_checked_at = now
                candidates.append(new_row)
                continue

            canonical = candidates[match_idx]
            existing_ids = set(canonical.source_message_ids)
            merged_ids = list(existing_ids | set(new_row.source_message_ids))
            canonical.source_message_ids = merged_ids
            new_row.dismissed_at = now
            merged += 1
            logger.info(
                "feature request merged business=%s duplicate=%s into=%s",
                business.id, new_row.id, canonical.id,
            )

    if merged:
        logger.info("merged %s duplicate feature request(s)", merged)
    return merged
