"""
The two team choices a business makes, kept in Business.settings["team"]:

  auto_assign_on_reply  the first agent to answer an unowned chat owns it (default on)
  routing               "manual" (default) - or "round_robin": a new chat goes to the
                        available agent who was handed one longest ago
  sla_minutes           first-reply target; None = no reminders
  agent_visibility      "all" - agents see every chat (default);
                        "assigned" - agents see their own chats and unowned ones
"""

DEFAULTS = {"auto_assign_on_reply": True, "agent_visibility": "all", "routing": "manual", "sla_minutes": None}
ROUTING = ("manual", "round_robin")
VISIBILITY = ("all", "assigned")


def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("team") or {})
    return {
        "auto_assign_on_reply": bool(raw.get("auto_assign_on_reply", DEFAULTS["auto_assign_on_reply"])),
        "agent_visibility": raw.get("agent_visibility") if raw.get("agent_visibility") in VISIBILITY
        else DEFAULTS["agent_visibility"],
        "routing": raw.get("routing") if raw.get("routing") in ROUTING else DEFAULTS["routing"],
        "sla_minutes": raw["sla_minutes"] if isinstance(raw.get("sla_minutes"), int) and raw["sla_minutes"] > 0 else None,
    }


def write(
    business, *, auto_assign_on_reply: bool, agent_visibility: str,
    routing: str = "manual", sla_minutes: int | None = None,
) -> dict:
    if agent_visibility not in VISIBILITY:
        raise ValueError("agent_visibility must be 'all' or 'assigned'")
    if routing not in ROUTING:
        raise ValueError("routing must be 'manual' or 'round_robin'")
    if sla_minutes is not None and not 1 <= sla_minutes <= 1440:
        raise ValueError("sla_minutes must be between 1 and 1440")
    value = {
        "auto_assign_on_reply": bool(auto_assign_on_reply), "agent_visibility": agent_visibility,
        "routing": routing, "sla_minutes": sla_minutes,
    }
    business.settings = {**(business.settings or {}), "team": value}
    return value
