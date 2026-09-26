"""
Type 2 (programs) - real extractor runs across all four sub-shapes.

Not a unit test: calls the real Claude-backed extractors (commitments.py and
signals.py) exactly as services/workers/analyse.py does, on Hinglish chats
modelled on how these businesses actually talk on WhatsApp. Prints what came
back next to what a careful human would expect, so the gaps are visible.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from shared.ai import commitments, signals

NOW = datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc)
TEMPLATE = json.load(open("shared/verticals/templates/programs.json", encoding="utf-8"))


def ctx(name: str, what: str) -> str:
    return f"Business: {name}. {what}\n{TEMPLATE['summary']}"


def convo(*turns):
    out, t = [], NOW - timedelta(hours=len(turns))
    for who, text in turns:
        out.append({
            "id": uuid.uuid4(),
            "direction": "inbound" if who == "C" else "outbound",
            "occurred_at": t,
            "text": text,
        })
        t += timedelta(minutes=7)
    return out


CASES = [
    # ── A. fixed course, instalments ─────────────────────────────────────
    ("A", "coaching instalment", ctx("Vidya Classes", "NEET coaching."),
     convo(("C", "Sir second instalment 8000 ka hai na? 10 tareekh tak kar dunga pakka"),
           ("B", "Ji 8,000 hi hai, 10 tak ho jaye to accha")),
     "they_owe payment 8000 due ~10 Oct"),
    ("A", "study abroad docs + fee", ctx("GlobalPath Overseas", "Study-abroad consultancy, Canada/UK admissions."),
     convo(("C", "Maine IELTS scorecard kal bhej dunga, aur application fee ka 25k Monday ko transfer karunga"),
           ("B", "Theek hai, scorecard milte hi hum SOP ka draft 3 din mein bhej denge")),
     "they_owe document (scorecard, tomorrow); they_owe payment 25000 Monday; we_owe document (SOP draft, 3 days after)"),
    ("A", "driving school slot", ctx("Safe Wheels Driving School", "21-day car driving course."),
     convo(("C", "Kal subah 7 baje wali class main aa jaunga"),
           ("B", "Done, instructor Ramesh 7 baje aapke ghar aa jayenge")),
     "they_owe meeting tomorrow 7am; we_owe meeting/visit tomorrow 7am"),

    # ── B. renewing membership ───────────────────────────────────────────
    ("B", "library renewal", ctx("Shanti Self-Study Library", "Monthly seat membership, reading room."),
     convo(("B", "Aapki seat ki membership 30 Sept ko khatam ho rahi hai, renew karna hai?"),
           ("C", "Haan renew karna hai, 1 tareekh ko 1200 de dunga")),
     "they_owe payment 1200 due 1 Oct (renewal)"),
    ("B", "PG leaving", ctx("Sunrise PG for Boys", "Paying guest, monthly rent."),
     convo(("C", "Bhaiya next month se main PG chhod raha hu, job lag gayi dusre city mein"),
           ("B", "Ok, 1 month notice aur deposit ka kya karna hai bata dena")),
     "churn signal (leaving); no payment promise"),
    ("B", "gym quiet drop", ctx("Iron House Gym", "Monthly/quarterly gym memberships."),
     convo(("C", "Bhai kuch din nahi aa paunga, time nahi mil raha, dekhta hu agle mahine"),),
     "churn risk (drifting); NOT a payment promise"),
    ("B", "tiffin pause", ctx("Maa Ki Rasoi Tiffin", "Monthly lunch tiffin subscription."),
     convo(("C", "Kal se 5 din ke liye tiffin band kar dena, ghar ja raha hu. Baaki paise adjust kar dena"),
           ("B", "Theek hai, 5 din pause kar diya")),
     "we_owe: pause 5 days / adjust amount (business promise); customer asked adjustment"),

    # ── C. session packages ──────────────────────────────────────────────
    ("C", "laser package", ctx("Glow Skin Studio", "Laser hair removal, 6-session packages."),
     convo(("C", "4th session ke baad bhi result nahi dikh raha, paise waste lag rahe hain"),
           ("B", "Sorry mam, doctor aapko kal call karke review karengi")),
     "complaint + churn risk; we_owe callback tomorrow"),
    ("C", "physio pack", ctx("FlexCare Physio", "10-session physiotherapy packs."),
     convo(("C", "Baaki 4 sessions next week se continue karunga, abhi 5000 ka balance Friday ko de dunga"),),
     "they_owe payment 5000 Friday; they_owe meeting next week"),

    # ── D. retainer / AMC ────────────────────────────────────────────────
    ("D", "CA retainer", ctx("Sharma & Co. Chartered Accountants", "Monthly GST + bookkeeping retainer."),
     convo(("B", "Sir September ki GST filing ke liye purchase bills 5 Oct tak bhej dijiye"),
           ("C", "Ok 4 tak bhej dunga. Aur last 2 months ki fees bhi usi din clear kar dunga")),
     "they_owe document bills by 4 Oct; they_owe payment (no amount) 4 Oct"),
    ("D", "AC AMC renewal", ctx("CoolCare AC Services", "Annual AC maintenance contracts (AMC)."),
     convo(("B", "Aapka AMC 15 Oct ko expire ho raha hai, renew ka 2400 hai"),
           ("C", "Abhi nahi, is saal nahi karwayenge")),
     "churn (declined renewal); no promise"),
    ("D", "agency unhappy", ctx("PixelPush Digital", "Social media management retainer, monthly."),
     convo(("C", "3 mahine ho gaye, leads bilkul nahi aa rahe. Kisi aur agency se baat kar rahe hain"),),
     "complaint + churn_risk + competitor evaluation"),
]


async def main():
    total_cost = 0
    for shape, name, business_context, messages, expected in CASES:
        c = await commitments.extract(messages=messages, business_context=business_context, now=NOW)
        s = await signals.extract(messages=messages, business_context=business_context, now=NOW)
        total_cost += c.cost_paise + s.cost_paise
        print(f"\n[{shape}] {name}")
        print(f"   expected : {expected}")
        for x in c.commitments:
            due = x.due_at.strftime("%d %b") if x.due_at else "-"
            amt = f"{x.amount_paise // 100}" if x.amount_paise else "-"
            kind = x.kind.value if hasattr(x.kind, "value") else x.kind
            dirn = x.direction.value if hasattr(x.direction, "value") else x.direction
            print(f"   commit   : {dirn} {kind} amt={amt} due={due} conf={x.confidence:.2f} | {x.description}")
        if not c.commitments:
            print("   commit   : (none)")
        for x in s.signals:
            print(f"   signal   : {x.kind}/{x.severity} | {x.title}")
        if not s.signals:
            print("   signal   : (none)")
    print(f"\ntotal model cost: Rs {total_cost / 100:.2f}")


asyncio.run(main())
