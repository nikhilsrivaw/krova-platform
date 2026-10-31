"""
The team choices a business makes, kept in Business.settings["team"]:

  auto_assign_on_reply  the first agent to answer an unowned chat owns it (default on)
  routing               "round_robin" (default) - a chat no team rule catches goes to the
                        available agent who was handed one longest ago; "manual" - nobody is assigned
  sla_minutes           first-reply target; None = no reminders
  agent_visibility      "all" - agents see every chat (default);
                        "assigned" - agents see their own chats and unowned ones

What agents (not the owner or admins) may see and do - docs/new/team-member-access-plan.md:

  agent_money           business-wide money: owed-to-you totals, pipeline values (default off)
  agent_payments        record a payment, send a payment link (default on)
  agent_phones          "shown" (default) or "last4" - customer numbers masked to the last 4 digits
  agent_templates       send an approved template once the 24-hour window has closed (default on)
"""

DEFAULTS = {
    "auto_assign_on_reply": True, "agent_visibility": "all", "routing": "round_robin", "sla_minutes": None,
    "agent_money": False, "agent_payments": True, "agent_phones": "shown", "agent_templates": True,
}
ROUTING = ("manual", "round_robin")
VISIBILITY = ("all", "assigned")
PHONES = ("shown", "last4")
SUPERVISORS = ("owner", "admin")


def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("team") or {})

    def flag(key: str) -> bool:
        return bool(raw.get(key, DEFAULTS[key]))

    return {
        "auto_assign_on_reply": flag("auto_assign_on_reply"),
        "agent_visibility": raw.get("agent_visibility") if raw.get("agent_visibility") in VISIBILITY
        else DEFAULTS["agent_visibility"],
        "routing": raw.get("routing") if raw.get("routing") in ROUTING else DEFAULTS["routing"],
        "sla_minutes": raw["sla_minutes"] if isinstance(raw.get("sla_minutes"), int) and raw["sla_minutes"] > 0 else None,
        "agent_money": flag("agent_money"),
        "agent_payments": flag("agent_payments"),
        "agent_phones": raw.get("agent_phones") if raw.get("agent_phones") in PHONES else DEFAULTS["agent_phones"],
        "agent_templates": flag("agent_templates"),
    }


def write(
    business, *, auto_assign_on_reply: bool, agent_visibility: str,
    routing: str = DEFAULTS["routing"], sla_minutes: int | None = None,
    agent_money: bool = DEFAULTS["agent_money"], agent_payments: bool = DEFAULTS["agent_payments"],
    agent_phones: str = DEFAULTS["agent_phones"], agent_templates: bool = DEFAULTS["agent_templates"],
) -> dict:
    if agent_visibility not in VISIBILITY:
        raise ValueError("agent_visibility must be 'all' or 'assigned'")
    if routing not in ROUTING:
        raise ValueError("routing must be 'manual' or 'round_robin'")
    if sla_minutes is not None and not 1 <= sla_minutes <= 1440:
        raise ValueError("sla_minutes must be between 1 and 1440")
    if agent_phones not in PHONES:
        raise ValueError("agent_phones must be 'shown' or 'last4'")
    value = {
        "auto_assign_on_reply": bool(auto_assign_on_reply), "agent_visibility": agent_visibility,
        "routing": routing, "sla_minutes": sla_minutes,
        "agent_money": bool(agent_money), "agent_payments": bool(agent_payments),
        "agent_phones": agent_phones, "agent_templates": bool(agent_templates),
    }
    business.settings = {**(business.settings or {}), "team": value}
    return value


def permissions(business, role: str | None) -> dict:
    """What this person may see and do - everything for the owner and admins, the switches for everyone else."""
    if role in SUPERVISORS:
        return {"see_money": True, "take_payments": True, "see_phones": True, "send_templates": True}
    s = read(business)
    return {
        "see_money": s["agent_money"], "take_payments": s["agent_payments"],
        "see_phones": s["agent_phones"] == "shown", "send_templates": s["agent_templates"],
    }
