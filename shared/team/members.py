"""
Adding, changing and removing the people on a business's team.

An owner or admin creates a teammate directly: a Team ID and a password, handed
over by whatever means the owner likes (most small businesses will just message
it). No email or phone is needed. The teammate signs in on the Team login page,
is made to choose their own password, and then works the inbox.

Who may do what to whom (the rule behind the split is in
docs/new/market-types-and-features.md section 4):

  * the owner can add admins and agents, reset anyone's password, change roles,
    and remove anyone but themselves;
  * an admin can add agents, reset an agent's password and remove agents;
  * an agent manages nobody;
  * nobody changes the owner, and nobody changes themselves here.

Every rule is enforced here rather than in the routes so it holds however this
is reached.
"""

import re
import secrets
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.passwords import hash_password
from shared.auth.service import revoke_all_sessions
from shared.db.models import (
    Business,
    BusinessMember,
    BusinessRole,
    Case,
    Customer,
    Escalation,
    PushSubscription,
    ThreadPresence,
    User,
)

MAX_MEMBERS = 25

_HANDLE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,29}$")

# No 0/o/1/l/i: a password read off one phone and typed on another.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class TeamError(Exception):
    """A refused team change. `status` is the HTTP code the route should use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def generate_password() -> str:
    """Three groups of four, e.g. 'k7mq-x9fd-3hpt' - easy to read out or copy."""
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def role_value(role) -> str:
    return role.value if hasattr(role, "value") else str(role)


def can_add(actor_role: str | None, new_role: str) -> bool:
    if new_role not in ("admin", "agent"):
        return False
    if actor_role == "owner":
        return True
    return actor_role == "admin" and new_role == "agent"


def can_manage(actor_role: str | None, target_role: str) -> bool:
    """Reset a password / remove: the owner anyone else, an admin only agents."""
    if target_role == "owner":
        return False
    if actor_role == "owner":
        return True
    return actor_role == "admin" and target_role == "agent"


def _slug_base(name: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", (name or "").lower())[:12]
    return base or "team"


async def team_slug(business: Business, db: AsyncSession) -> str:
    """
    The part of every Team ID after the '@' - chosen once per business and kept,
    so renaming the business never changes anyone's login.
    """
    settings = dict(business.settings or {})
    existing = settings.get("team_slug")
    if existing:
        return existing

    base = _slug_base(business.name)
    candidate = base
    for n in range(2, 100):
        taken = await db.execute(
            select(func.count(Business.id)).where(
                Business.settings["team_slug"].as_string() == candidate,
                Business.id != business.id,
            )
        )
        if int(taken.scalar_one()) == 0:
            break
        candidate = f"{base}{n}"
    settings["team_slug"] = candidate
    business.settings = settings
    return candidate


def clean_handle(handle: str) -> str:
    h = (handle or "").strip().lower().replace(" ", ".")
    if not _HANDLE.match(h):
        raise TeamError(
            "The ID can use letters, numbers, dots, dashes and underscores "
            "(2 to 30 characters), for example 'rahul' or 'rahul.counter'."
        )
    return h


async def _member_count(business_id: uuid.UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(BusinessMember.user_id)).where(BusinessMember.business_id == business_id)
    )
    return int(result.scalar_one())


async def create_member(
    db: AsyncSession, *, business: Business, actor_id: uuid.UUID, actor_role: str | None,
    full_name: str, handle: str, role: str, password: str | None = None,
) -> tuple[User, str, str]:
    """Create the account and its membership. Returns (user, team_id, the password to hand over)."""
    if not can_add(actor_role, role):
        raise TeamError(
            "Only the owner can add an admin." if role == "admin" else "You cannot add team members.",
            403,
        )
    name = (full_name or "").strip()
    if not name:
        raise TeamError("Enter the person's name.")
    if await _member_count(business.id, db) >= MAX_MEMBERS:
        raise TeamError(f"A team can have up to {MAX_MEMBERS} people.", 409)

    slug = await team_slug(business, db)
    team_id = f"{clean_handle(handle)}@{slug}"
    clash = await db.execute(select(User.id).where(User.username == team_id))
    if clash.first() is not None:
        raise TeamError("That Team ID is already taken. Choose another name.", 409)

    plain = password or generate_password()
    user = User(
        username=team_id,
        password_hash=hash_password(plain),
        full_name=name,
        is_active=True,
        must_change_password=True,
        created_by_user_id=actor_id,
    )
    db.add(user)
    await db.flush()
    db.add(BusinessMember(business_id=business.id, user_id=user.id, role=BusinessRole(role)))
    await db.flush()
    return user, team_id, plain


async def _membership(business_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> BusinessMember:
    row = await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.user_id == user_id
        )
    )
    member = row.scalar_one_or_none()
    if member is None:
        raise TeamError("That person is not on this team.", 404)
    return member


async def reset_password(
    db: AsyncSession, *, business_id: uuid.UUID, actor_id: uuid.UUID, actor_role: str | None,
    user_id: uuid.UUID, password: str | None = None,
) -> tuple[User, str]:
    """New password for a teammate; signs them out everywhere and clears any lock."""
    if user_id == actor_id:
        raise TeamError("Change your own password from your account settings.", 400)
    member = await _membership(business_id, user_id, db)
    if not can_manage(actor_role, role_value(member.role)):
        raise TeamError("You cannot manage this person.", 403)
    user = await db.get(User, user_id)
    if user is None or user.username is None:
        # Someone who signed up themselves owns their own credentials.
        raise TeamError("This person signs in with their own email, so only they can change it.", 400)
    plain = password or generate_password()
    user.password_hash = hash_password(plain)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    await revoke_all_sessions(user.id, db)
    return user, plain


async def change_role(
    db: AsyncSession, *, business_id: uuid.UUID, actor_id: uuid.UUID, actor_role: str | None,
    user_id: uuid.UUID, role: str,
) -> BusinessMember:
    if actor_role != "owner":
        raise TeamError("Only the owner can change a role.", 403)
    if role not in ("admin", "agent"):
        raise TeamError("Choose admin or agent.")
    if user_id == actor_id:
        raise TeamError("You cannot change your own role.", 400)
    member = await _membership(business_id, user_id, db)
    if role_value(member.role) == "owner":
        raise TeamError("The owner's role cannot be changed.", 403)
    member.role = BusinessRole(role)
    # Their open sessions carry the old role in the token; the API re-reads it
    # from the database on every request, so it applies from their next click.
    return member


async def release_work(db: AsyncSession, *, business_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    """Hand a person's open work back to the pool so nothing sits with someone who can't act."""
    chats = await db.execute(
        update(Customer)
        .where(Customer.business_id == business_id, Customer.assigned_to_user_id == user_id)
        .values(assigned_to_user_id=None, assigned_at=None)
    )
    cases = await db.execute(
        update(Case)
        .where(Case.business_id == business_id, Case.assigned_to_user_id == user_id)
        .values(assigned_to_user_id=None)
    )
    escalations = await db.execute(
        update(Escalation)
        .where(
            Escalation.business_id == business_id,
            Escalation.assigned_to_user_id == user_id,
            Escalation.status.in_(("open", "in_progress")),
        )
        .values(assigned_to_user_id=None, assigned_at=None)
    )
    await db.execute(
        delete(ThreadPresence).where(
            ThreadPresence.business_id == business_id, ThreadPresence.user_id == user_id
        )
    )
    return {
        "conversations": chats.rowcount or 0,
        "cases": cases.rowcount or 0,
        "escalations": escalations.rowcount or 0,
    }


async def remove_member(
    db: AsyncSession, *, business_id: uuid.UUID, actor_id: uuid.UUID, actor_role: str | None,
    user_id: uuid.UUID,
) -> dict:
    """
    Take a person off the team. Their access stops at once (the API re-checks
    membership on every request), their open work goes back to the pool, their
    sign-ins and push devices are revoked. The account itself is kept disabled
    when it only ever belonged here, so past activity still names them.
    """
    if user_id == actor_id:
        raise TeamError("You cannot remove yourself.", 400)
    member = await _membership(business_id, user_id, db)
    if not can_manage(actor_role, role_value(member.role)):
        raise TeamError("You cannot remove this person.", 403)

    released = await release_work(db, business_id=business_id, user_id=user_id)
    await db.delete(member)
    await db.flush()
    await revoke_all_sessions(user_id, db)
    await db.execute(delete(PushSubscription).where(PushSubscription.user_id == user_id))

    others = await db.execute(
        select(func.count(BusinessMember.user_id)).where(BusinessMember.user_id == user_id)
    )
    user = await db.get(User, user_id)
    if user is not None and user.username is not None and int(others.scalar_one()) == 0:
        user.is_active = False
    return released


async def transfer_ownership(
    db: AsyncSession, *, business_id: uuid.UUID, owner_id: uuid.UUID, actor_role: str | None,
    password: str, to_user_id: uuid.UUID,
) -> None:
    """
    Hand the business to an existing admin; the old owner becomes an admin.

    Needs the owner's own password again - a left-open phone must not be able to
    give the business away. Only to an admin (promote first), so ownership never
    jumps straight to someone the owner has not already trusted with settings.
    """
    from shared.auth.passwords import verify_password

    if actor_role != "owner":
        raise TeamError("Only the owner can transfer ownership.", 403)
    if to_user_id == owner_id:
        raise TeamError("You already own this business.", 400)
    me = await db.get(User, owner_id)
    if me is None or not verify_password(password, me.password_hash):
        raise TeamError("Your password is incorrect.", 403)
    target = await _membership(business_id, to_user_id, db)
    if role_value(target.role) != "admin":
        raise TeamError("Make them an admin first, then transfer ownership.", 400)
    mine = await _membership(business_id, owner_id, db)
    target.role = BusinessRole.owner
    mine.role = BusinessRole.admin
    await db.flush()
