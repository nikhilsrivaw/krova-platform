"""
Re-encrypt every stored credential with the current ENCRYPTION_KEY.

Why: if the key may have leaked, or is simply old, it has to be replaced - and every channel token, payment key and
PIN stored with it must be re-encrypted, or they stop working. Those values live in many tables and inside JSON
settings, so this does not keep a list that could miss one: it walks every text and JSON column of every table and
re-encrypts whatever is a Fernet token (they all start "gAAAAA").

How to rotate (on the server, one step at a time):
  1. Make a new key:
       docker compose -f docker-compose.prod.yml exec -T app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. In .env: ENCRYPTION_KEY=<new key>, ENCRYPTION_KEYS_OLD=<old key>. Restart. Everything keeps working - old
     values are read with the old key, new ones are written with the new.
  3. See what would change (writes nothing):
       docker compose -f docker-compose.prod.yml exec -T app python -m scripts.rotate_encryption_key
  4. Do it:
       docker compose -f docker-compose.prod.yml exec -T app python -m scripts.rotate_encryption_key --apply
  5. Run step 3 again: it must say 0 to rotate and 0 failed. Then remove ENCRYPTION_KEYS_OLD from .env and restart.
Keep both keys somewhere safe (a password manager) until step 5 is done.
"""

import asyncio
import re
import sys
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from shared.auth import encryption
from shared.config.settings import settings

_TOKEN = re.compile(r"^gAAAAA[A-Za-z0-9_\-]{40,}={0,2}$")


def looks_encrypted(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOKEN.match(value))


def needs_rotation(value: str, current: Fernet) -> bool:
    """A token the current key cannot open was stored with an earlier key."""
    try:
        current.decrypt(value.encode())
        return False
    except InvalidToken:
        return True


def rotate_value(value: Any, current: Fernet, stats: dict) -> Any:
    """The value with every token under an earlier key re-encrypted, walking into JSON. Never raises."""
    if isinstance(value, dict):
        return {k: rotate_value(v, current, stats) for k, v in value.items()}
    if isinstance(value, list):
        return [rotate_value(v, current, stats) for v in value]
    if looks_encrypted(value) and needs_rotation(value, current):
        try:
            rotated = encryption.rotate(value)
        except encryption.DecryptionError:
            stats["failed"] += 1
            return value
        stats["rotated"] += 1
        return rotated
    return value


async def run(apply: bool) -> dict:
    from sqlalchemy import JSON, String, Text, select, update
    from sqlalchemy.dialects.postgresql import JSONB

    import shared.db.models  # noqa: F401 - registers every table
    from shared.db.base import Base
    from shared.db.session import AsyncSessionLocal

    current = Fernet(settings.encryption_key.encode())
    totals = {"rotated": 0, "failed": 0}
    async with AsyncSessionLocal() as db:
        for table in Base.metadata.sorted_tables:
            columns = [c for c in table.columns if isinstance(c.type, (String, Text, JSON, JSONB))]
            keys = list(table.primary_key.columns)
            if not columns or not keys:
                continue
            rows = (await db.execute(select(*keys, *columns))).all()
            stats = {"rotated": 0, "failed": 0}
            for row in rows:
                changes = {}
                for col in columns:
                    old = row._mapping[col]
                    new = rotate_value(old, current, stats)
                    if new != old:
                        changes[col.name] = new
                if changes and apply:
                    where = [k == row._mapping[k] for k in keys]
                    await db.execute(update(table).where(*where).values(**changes))
            if stats["rotated"] or stats["failed"]:
                print(f"{table.name}: {stats['rotated']} to rotate, {stats['failed']} failed")
            totals["rotated"] += stats["rotated"]
            totals["failed"] += stats["failed"]
        if apply:
            await db.commit()
    verb = "rotated" if apply else "to rotate"
    print(f"total: {totals['rotated']} {verb}, {totals['failed']} failed (failed = no configured key opens it)")
    return totals


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
