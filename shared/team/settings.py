"""
The two team choices a business makes, kept in Business.settings["team"]:

  auto_assign_on_reply  the first agent to answer an unowned chat owns it (default on)
  agent_visibility      "all" - agents see every chat (default);
                        "assigned" - agents see their own chats and unowned ones
"""

DEFAULTS = {"auto_assign_on_reply": True, "agent_visibility": "all"}
VISIBILITY = ("all", "assigned")


def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("team") or {})
    return {
        "auto_assign_on_reply": bool(raw.get("auto_assign_on_reply", DEFAULTS["auto_assign_on_reply"])),
        "agent_visibility": raw.get("agent_visibility") if raw.get("agent_visibility") in VISIBILITY
        else DEFAULTS["agent_visibility"],
    }


def write(business, *, auto_assign_on_reply: bool, agent_visibility: str) -> dict:
    if agent_visibility not in VISIBILITY:
        raise ValueError("agent_visibility must be 'all' or 'assigned'")
    value = {"auto_assign_on_reply": bool(auto_assign_on_reply), "agent_visibility": agent_visibility}
    business.settings = {**(business.settings or {}), "team": value}
    return value
