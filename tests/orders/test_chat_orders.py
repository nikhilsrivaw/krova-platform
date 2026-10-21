"""Taking an order in a chat - shared/catalogue/chat_orders.py."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from shared.catalogue import chat_orders
from shared.db.models import CommitmentStatus, OrderStatus
from shared.payments import links

BIZ = uuid.uuid4()


def _variant(title="M", price=89900, available=True):
    return SimpleNamespace(id=uuid.uuid4(), product_id=uuid.uuid4(), title=title, sku="K-M", price_paise=price,
                           available=available)


class _VariantDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        rows = self.rows
        return SimpleNamespace(all=lambda: rows)


def _build(rows, lines):
    return asyncio.run(chat_orders.build_items(_VariantDb(rows), BIZ, lines))


def test_lines_are_priced_from_the_product_list():
    kurta, dupatta = _variant("M", 89900), _variant("Default Title", 49900)
    items = _build([(kurta, "Kurta"), (dupatta, "Dupatta")], [(kurta.id, 2), (dupatta.id, 1)])
    assert [(i["title"], i["quantity"], i["price_paise"]) for i in items] == [("Kurta - M", 2, 89900), ("Dupatta", 1, 49900)]
    assert chat_orders.total(items) == 229700
    assert chat_orders.label(items) == "2 × Kurta - M, Dupatta"


def test_a_product_without_a_price_is_refused():
    v = _variant(price=None)
    with pytest.raises(chat_orders.OrderError, match="no price"):
        _build([(v, "Kurta")], [(v.id, 1)])


def test_an_unavailable_product_is_refused():
    v = _variant(available=False)
    with pytest.raises(chat_orders.OrderError, match="unavailable"):
        _build([(v, "Kurta")], [(v.id, 1)])


def test_a_product_from_another_business_or_removed_is_refused():
    with pytest.raises(chat_orders.OrderError, match="no longer"):
        _build([], [(uuid.uuid4(), 1)])


def test_nothing_picked_is_refused():
    with pytest.raises(chat_orders.OrderError):
        _build([], [])


def test_a_whatsapp_cart_is_labelled_by_count():
    assert chat_orders.label([{"product_retailer_id": "a1", "quantity": 2}, {"product_retailer_id": "b2"}]) == "3 items"


class _Db:
    def __init__(self, order=None):
        self.order, self.added = order, []

    async def get(self, model, key):
        return self.order

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


def test_a_cod_order_taken_in_chat_is_already_confirmed():
    db = _Db()
    customer = SimpleNamespace(id=uuid.uuid4())
    items = [{"title": "Kurta", "quantity": 1, "price_paise": 89900}]
    order = asyncio.run(chat_orders.create(db, business=SimpleNamespace(id=BIZ), customer=customer, items=items,
                                           cod=True, note=" gift wrap ", user_id=None))
    assert order.source_platform == "chat" and order.is_cod and order.cod_confirmed_at is not None
    assert order.status == OrderStatus.pending and order.total_paise == 89900 and order.order_number.startswith("WA-")
    assert order.raw_payload["note"] == "gift wrap"


def test_a_prepaid_order_is_not_marked_confirmed():
    order = asyncio.run(chat_orders.create(_Db(), business=SimpleNamespace(id=BIZ), customer=SimpleNamespace(id=uuid.uuid4()),
                                           items=[{"title": "x", "quantity": 1, "price_paise": 100}], cod=False, note=None, user_id=None))
    assert not order.is_cod and order.cod_confirmed_at is None


def _order(**kw):
    base = dict(id=uuid.uuid4(), business_id=BIZ, source_platform="chat", customer_id=uuid.uuid4(), status=OrderStatus.pending, total_paise=229700,
                order_number="WA-1A2B3C", items=[{"title": "Kurta - M", "quantity": 2, "price_paise": 89900}], is_cod=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _link(monkeypatch, order, existing=None, open_link=None):
    made = {}

    async def fee_commitment_for(db, **kwargs):
        made.update(kwargs)
        return existing or SimpleNamespace(id=uuid.uuid4(), status=CommitmentStatus.open, outstanding_paise=kwargs["amount_paise"])

    async def open_link_for(db, commitment_id, now):
        return open_link

    async def send_link(db, **kwargs):
        return SimpleNamespace(id="l"), True, None

    monkeypatch.setattr(links, "_fee_commitment_for", fee_commitment_for)
    monkeypatch.setattr(links, "_open_link_for", open_link_for)
    async def summary_text(db, order):
        return "summary"

    monkeypatch.setattr(links, "send_link", send_link)
    monkeypatch.setattr(chat_orders, "summary_text", summary_text)
    out = asyncio.run(chat_orders.send_link(_Db(order), business=SimpleNamespace(id=BIZ), order=order, user_id=None))
    return out, made


def test_the_link_asks_for_the_order_total_and_names_the_items(monkeypatch):
    (link, sent, _), made = _link(monkeypatch, _order())
    assert sent and made["amount_paise"] == 229700 and made["source_system"] == chat_orders.ORDER_PAYMENT
    assert made["description"] == "Order WA-1A2B3C: 2 × Kurta - M"


def test_a_paid_order_gets_no_link(monkeypatch):
    with pytest.raises(links.LinkError, match="unpaid"):
        _link(monkeypatch, _order(status=OrderStatus.paid))


def test_a_second_link_for_the_same_order_is_the_open_one(monkeypatch):
    open_link = SimpleNamespace(id="existing")
    (link, sent, reason), _ = _link(monkeypatch, _order(), open_link=open_link)
    assert link is open_link and not sent and "already sent" in reason


def test_a_fully_paid_order_payment_marks_the_order_paid(monkeypatch):
    order = _order()
    fired = []

    async def fire_safely(db, **kwargs):
        fired.append(kwargs["context"]["status"])

    async def conversion(db, o):
        fired.append("conversion")

    from shared.care import post_call_actions
    monkeypatch.setattr(post_call_actions, "fire_safely", fire_safely)
    monkeypatch.setattr(chat_orders, "_purchase_conversion", conversion)
    commitment = SimpleNamespace(source_system=chat_orders.ORDER_PAYMENT, status=CommitmentStatus.met,
                                 external_ref=str(order.id), business_id=BIZ)
    assert asyncio.run(chat_orders.settle_if_paid(_Db(order), commitment)) is order
    assert order.status == OrderStatus.paid and fired == ["paid", "conversion"]


def test_a_part_payment_or_another_commitment_leaves_the_order_alone():
    order = _order()
    part = SimpleNamespace(source_system=chat_orders.ORDER_PAYMENT, status=CommitmentStatus.open, external_ref=str(order.id), business_id=BIZ)
    fee = SimpleNamespace(source_system=links.TOKEN_FEE, status=CommitmentStatus.met, external_ref=str(order.id), business_id=BIZ)
    assert asyncio.run(chat_orders.settle_if_paid(_Db(order), part)) is None
    assert asyncio.run(chat_orders.settle_if_paid(_Db(order), fee)) is None
    assert order.status == OrderStatus.pending


def test_a_whatsapp_cart_order_gets_a_link_too(monkeypatch):
    (link, sent, _), made = _link(monkeypatch, _order(source_platform="whatsapp", order_number=None,
                                                      items=[{"product_retailer_id": "a1", "quantity": 2}]))
    assert sent and made["description"] == "WhatsApp order: 2 items"


def test_a_shopify_order_is_never_sent_a_link(monkeypatch):
    with pytest.raises(links.LinkError, match="its own store"):
        _link(monkeypatch, _order(source_platform="shopify"))


class _PolicyDb:
    def __init__(self, policy):
        self.policy = policy

    async def execute(self, statement):
        policy = self.policy
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: policy))


def test_the_summary_breaks_down_the_price_and_states_the_return_policy():
    order = _order(is_cod=True, items=[{"title": "Kurta - M", "quantity": 2, "price_paise": 89900},
                                       {"title": "Dupatta", "quantity": 1, "price_paise": 49900}], total_paise=229700)
    text = asyncio.run(chat_orders.summary_text(_PolicyDb("Exchange within 7 days, unworn."), order))
    assert text.splitlines() == [
        "Order WA-1A2B3C", "2 × Kurta - M - Rs 1,798", "1 × Dupatta - Rs 499", "Total: Rs 2,297",
        "Payment: cash on delivery / delivery par cash", "Returns and refunds: Exchange within 7 days, unworn.",
    ]


def test_no_policy_on_the_card_no_policy_line():
    text = asyncio.run(chat_orders.summary_text(_PolicyDb(None), _order()))
    assert "Returns" not in text and text.endswith("Total: Rs 2,297")
