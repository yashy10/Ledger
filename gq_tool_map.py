"""
gq_tool_map.py — Map an MCP tool call onto a Ledger action.

The gateway can't know a downstream server's exact tool names ahead of time, so
this mapping is keyword-driven over the tool name, with the risky parameters
pulled out of the call arguments. It returns the `action` dict that Ledger's
`/agent/action` expects: {type, category, cost_usd, description}.

The emitted `category` values MUST match the policies seeded by gq_setup.py:
    research_read         -> no rule          (auto-approve)
    participant_recruiting-> intent_alignment (only the LLM can catch off-task)
    incentive_payout      -> category_block + budget_cap
    pii_export            -> category_block (threshold 0; needs nonzero cost)
    bulk_messaging        -> category_block (recipient volume)

Cost is a proxy for risk/scale, tuned so the demo lands:
  - recruit: count * $5      -> 40 participants = $200  (>= $100 intent floor,
                                                         < $1,000 budget cap, so
                                                         ONLY intent_alignment can
                                                         flag an off-task recruit)
  - incentive: amount * recipients                       (trips at > $50)
  - pii_export: $1 nominal   -> nonzero so a threshold-0 category_block fires
                                (engine uses strict cost > threshold, main.py:605)
  - bulk: recipients * $0.10 -> 200 recipients = $20     (trips at > $5, ~50+)

This module is pure and must never raise: unknown tools degrade to a benign read.
"""
from typing import Any

RECRUIT_UNIT_COST = 5.0
BULK_UNIT_COST = 0.10
PII_NOMINAL_COST = 1.0  # nonzero on purpose — see module docstring


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(args: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in args and args[k] is not None:
            return args[k]
    return default


def _count(args: dict, *keys: str, default: int = 1) -> int:
    """Best-effort count: an explicit numeric arg, or the length of a list arg."""
    for k in keys:
        v = args.get(k)
        if isinstance(v, (list, tuple)):
            return len(v)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return default


def map_tool_to_action(tool_name: str, arguments: dict) -> dict:
    name = (tool_name or "").lower()
    args = arguments or {}

    def has(*subs: str) -> bool:
        return any(s in name for s in subs)

    # Order matters: check specific/dangerous intents before generic ones.
    # (e.g. "send_incentive" must match incentive before the "send"/message rule.)

    # 1) PII / data export
    if has("export", "download", "csv", "dump", "pii", "participant_data"):
        n = _count(args, "count", "num", "limit", "participant_ids", "ids", default=0)
        fields = _first(args, "fields", "columns", default=[])
        fdesc = ",".join(map(str, fields)) if isinstance(fields, (list, tuple)) else str(fields)
        who = f" for {n} participants" if n else ""
        extra = f" (fields: {fdesc})" if fdesc else ""
        return {
            "type": "export",
            "category": "pii_export",
            "cost_usd": PII_NOMINAL_COST,
            "description": f"Export participant data{who}{extra}".strip(),
        }

    # 2) Money: incentives / payouts / rewards
    if has("incentive", "payout", "reward", "compensat", "pay"):
        amount = _num(_first(args, "amount_usd", "amount", "incentive_usd", "value"), 0.0)
        recipients = _count(args, "participant_ids", "recipients", "count", "ids", default=1)
        cost = amount * max(recipients, 1) if amount else _num(_first(args, "total_usd"), 0.0)
        return {
            "type": "payment",
            "category": "incentive_payout",
            "cost_usd": cost,
            "description": f"Pay ${amount:,.2f} incentive to {recipients} participant(s)",
        }

    # 3) Recruiting / sourcing participants
    if has("recruit", "source_participants", "panel", "screen"):
        count = _count(args, "count", "n", "num_participants", "quota", "target", default=1)
        segment = _first(args, "segment", "audience", "criteria", "profile", default="")
        seg = f" in segment: {segment}" if segment else ""
        return {
            "type": "recruit",
            "category": "participant_recruiting",
            "cost_usd": count * RECRUIT_UNIT_COST,
            "description": f"Recruit {count} participants{seg}",
        }

    # 4) Bulk outreach / messaging / email / invites
    if has("message", "email", "invite", "notify", "bulk", "sms", "outreach", "campaign", "send"):
        recipients = _count(args, "recipients", "count", "audience_size", "to", "participant_ids", default=1)
        segment = _first(args, "segment", "audience", default="")
        seg = f" ({segment})" if segment else ""
        return {
            "type": "email",
            "category": "bulk_messaging",
            "cost_usd": recipients * BULK_UNIT_COST,
            "description": f"Bulk message to {recipients} recipients{seg}",
        }

    # 5) Reads / everything else -> benign, auto-approve
    q = _first(args, "query", "study_id", "id", "q", default="")
    qd = f": {q}" if q else ""
    return {
        "type": "read",
        "category": "research_read",
        "cost_usd": 0.0,
        "description": f"{tool_name}{qd}".strip(),
    }
