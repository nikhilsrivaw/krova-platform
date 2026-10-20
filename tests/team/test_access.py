"""
Which customers a team member may see (shared/team/access.py), and that the inbox, ledger,
approvals and escalations actually apply it. No database: queries are compiled to SQL and run
against an empty stand-in.
"""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.db.models import Business, Customer  # noqa: E402
from shared.team import access  # noqa: E402

BIZ = uuid.uuid4()
ME = uuid.uuid4()
MATE = uuid.uuid4()


def _user(role="agent"):
    return SimpleNamespace(id=ME, business=BIZ, role=role, email="a@example.com")


def _customer(**over):
    base = dict(id=uuid.uuid4(), business_id=BIZ, is_private=False, assigned_to_user_id=None, display_name="Asha")
    base.update(over)
    return SimpleNamespace(**base)


class _Result:
    def scalars(self):
        return SimpleNamespace(all=lambda: [], first=lambda: None)

    def scalar_one(self):
        return 0

    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def all(self):
        return []

    def one(self):
        return (0, 0)


class _Db:
    def __init__(self, visibility="all", customer=None):
        self.business = SimpleNamespace(id=BIZ, settings={"team": {"agent_visibility": visibility}})
        self.customer = customer
        self.scalar = None
        self.sql: list[str] = []

    async def get(self, model, key, **_):
        if model is Business:
            return self.business
        if model is Customer:
            return self.customer
        return None

    async def execute(self, stmt):
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Result(self.scalar)


def run(coro):
    return asyncio.run(coro)


def where(sql: str) -> str:
    """Only the filter - every select of customers lists is_private as a column."""
    return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""


# ── the rule itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_see_everyone(role):
    db = _Db("assigned")
    assert run(access.customer_rule(db, _user(role))) is None
    assert run(access.visible_ids(db, _user(role))) is None
    assert run(access.can_see(db, _user(role), _customer(is_private=True, assigned_to_user_id=MATE)))


def test_an_agent_never_sees_a_private_chat_even_when_agents_see_everything():
    db = _Db("all")
    assert not run(access.can_see(db, _user(), _customer(is_private=True)))
    with pytest.raises(HTTPException) as err:
        run(access.check(db, _user(), _customer(is_private=True)))
    assert err.value.status_code == 403 and "private" in err.value.detail


def test_with_agents_seeing_everything_a_teammates_chat_is_visible():
    assert run(access.can_see(_Db("all"), _user(), _customer(assigned_to_user_id=MATE)))


def test_own_only_hides_a_teammates_chat_but_not_mine_or_an_unowned_one():
    db = _Db("assigned")
    assert not run(access.can_see(db, _user(), _customer(assigned_to_user_id=MATE)))
    assert run(access.can_see(db, _user(), _customer(assigned_to_user_id=ME)))
    assert run(access.can_see(db, _user(), _customer(assigned_to_user_id=None)))
    with pytest.raises(HTTPException) as err:
        run(access.check(db, _user(), _customer(assigned_to_user_id=MATE)))
    assert err.value.detail == "This chat is with a teammate."


def test_the_sql_rule_matches():
    def compiled(rule):
        return str(rule.compile(dialect=postgresql.dialect()))

    everyone = compiled(run(access.customer_rule(_Db("all"), _user())))
    assert "is_private" in everyone and "assigned_to_user_id" not in everyone
    own = compiled(run(access.customer_rule(_Db("assigned"), _user())))
    assert "is_private" in own and "assigned_to_user_id" in own


def test_check_id_refuses_another_business_and_lets_an_unknown_caller_through():
    assert run(access.check_id(_Db(), _user(), None)) is None
    with pytest.raises(HTTPException) as err:
        run(access.check_id(_Db(customer=_customer(business_id=uuid.uuid4())), _user(), uuid.uuid4()))
    assert err.value.status_code == 404


# ── the screens apply it ────────────────────────────────────────────────


def test_inbox_ignores_include_private_for_an_agent():
    from services.api.routers import conversations

    db = _Db("all")
    run(conversations.list_conversations(_user(), db, limit=50, include_private=True, q=None))
    assert "is_private" in where(db.sql[0])

    db = _Db("all")
    run(conversations.list_conversations(_user("owner"), db, limit=50, include_private=True, q=None))
    assert "is_private" not in where(db.sql[0])


def test_a_private_thread_is_refused_to_an_agent():
    from services.api.routers import conversations

    db = _Db("all", customer=_customer(is_private=True))
    with pytest.raises(HTTPException) as err:
        run(conversations.get_thread(uuid.uuid4(), _user(), db, limit=200))
    assert err.value.status_code == 403


def test_customers_promises_and_totals_follow_own_only():
    from services.api.routers import ledger

    db = _Db("assigned")
    run(ledger.list_customers(_user(), db, limit=50, q=None, customer_id=None))
    assert "assigned_to_user_id" in where(db.sql[0])

    db = _Db("assigned")
    run(ledger.list_commitments(_user(), db, direction=None, status_filter=None, overdue_only=False,
                                customer_id=None, limit=50))
    assert "assigned_to_user_id" in where(db.sql[0])

    db = _Db("assigned")
    db.business.settings["team"]["agent_money"] = True
    run(ledger.ledger_summary(_user(), db))
    assert all("assigned_to_user_id" in where(s) for s in db.sql)


def test_approvals_and_escalations_follow_the_rule():
    from services.api.routers import approvals, escalations

    db = _Db("assigned")
    run(approvals.list_drafts(_user(), db, status_filter="pending", limit=50))
    assert "assigned_to_user_id" in where(db.sql[0])

    db = _Db("all")
    run(approvals.pending_count(_user(), db))
    assert all("is_private" in where(s) for s in db.sql)

    db = _Db("assigned")
    run(escalations.list_escalations(_user(), db, acknowledged=False, mine=False, unassigned=False))
    assert "assigned_to_user_id" in where(db.sql[0]) and "is_private" in where(db.sql[0])


def test_owner_lists_are_not_narrowed():
    from services.api.routers import approvals, ledger

    db = _Db("assigned")
    run(approvals.list_drafts(_user("owner"), db, status_filter="pending", limit=50))
    run(ledger.list_customers(_user("admin"), db, limit=50, q=None, customer_id=None))
    assert not any("is_private" in where(s) for s in db.sql)


# ── what team members may do (Team page switches) ──────────────────────


def test_default_permissions_for_an_agent_and_everything_for_the_owner():
    from shared.team import settings as ts

    biz = SimpleNamespace(settings={})
    assert ts.permissions(biz, "agent") == {
        "see_money": False, "take_payments": True, "see_phones": True, "send_templates": True,
    }
    biz = SimpleNamespace(settings={"team": {"agent_money": True, "agent_payments": False,
                                             "agent_phones": "last4", "agent_templates": False}})
    assert ts.permissions(biz, "agent") == {
        "see_money": True, "take_payments": False, "see_phones": False, "send_templates": False,
    }
    assert all(ts.permissions(biz, "admin").values()) and all(ts.permissions(None, "owner").values())


def test_business_totals_are_refused_to_an_agent_by_default_but_not_to_the_owner():
    from services.api.routers import ledger

    with pytest.raises(HTTPException) as err:
        run(ledger.ledger_summary(_user(), _Db()))
    assert err.value.status_code == 403
    run(ledger.ledger_summary(_user("owner"), _Db()))


def test_payments_switched_off_refuses_a_payment_link():
    from services.api.routers import payments

    db = _Db()
    db.business.settings["team"]["agent_payments"] = False
    with pytest.raises(HTTPException) as err:
        run(payments._send_fee(db, _user(), "token", uuid.uuid4(), SimpleNamespace()))
    assert err.value.status_code == 403 and "payments" in err.value.detail


def test_templates_switched_off_refuses_a_template_send():
    from services.api.routers import messages

    db = _Db()
    db.business.settings["team"]["agent_templates"] = False
    body = messages.SendTemplate(to="+919876543210", template_name="hello")
    with pytest.raises(HTTPException) as err:
        run(messages.send_template(body, _user(), db))
    assert err.value.status_code == 403


def test_masked_numbers_keep_the_last_four_digits_only():
    assert access.mask("+919876543210") == "••••••3210"
    assert access.mask(None) is None
    out = access.identities_out(
        [{"kind": "phone", "value": "+919876543210"}, {"kind": "email", "value": "a@b.in"}], hide=True,
    )
    assert out[0] == {"kind": "phone", "value": "••••••3210", "masked": True}
    assert out[1] == {"kind": "email", "value": "a@b.in"}
    assert access.identities_out(out[1:], hide=False) == out[1:]


def test_a_masked_number_is_sent_to_by_customer_id():
    from services.api.routers import messages

    db = _Db(customer=_customer())
    db.scalar = "+919876543210"
    assert run(messages._recipient(f"customer:{uuid.uuid4()}", _user(), db)) == "+919876543210"
    assert run(messages._recipient("+91 98765 43210", _user(), db)) == messages.normalise_phone("+919876543210")

    db = _Db(customer=_customer(is_private=True))
    with pytest.raises(HTTPException) as err:
        run(messages._recipient(f"customer:{uuid.uuid4()}", _user(), db))
    assert err.value.status_code == 403
