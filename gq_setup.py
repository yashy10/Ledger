"""
gq_setup.py — Seed the "great-question" project + governance policies into a
running Ledger server, idempotently.

This configures Ledger for the Great Question MCP demo: it creates (and
activates) a dedicated project so the GQ policies don't collide with the default
project, then seeds the rules the MCP gateway relies on — including the
intent_alignment rule (which is NOT seeded by Ledger's defaults and is required
for the "off-task action caught by the LLM" showpiece).

Safe to run repeatedly: existing projects/policies are detected and skipped.

Usage:
    python3 gq_setup.py                       # against http://localhost:8000
    python3 gq_setup.py http://host:8000
    LEDGER_URL=http://host:8000 python3 gq_setup.py
    python3 gq_setup.py --reset-learning      # also clear learned auto-approvals
                                              # (run before a live demo so escalations
                                              #  reliably fire instead of auto-approving)
"""
import os
import sys

import requests

_args = [a for a in sys.argv[1:] if not a.startswith("-")]
_flags = {a for a in sys.argv[1:] if a.startswith("-")}
LEDGER_URL = (
    _args[0].rstrip("/") if _args
    else os.environ.get("LEDGER_URL", "http://localhost:8000").rstrip("/")
)
RESET_LEARNING = "--reset-learning" in _flags
PROJECT_ID = "great-question"
PROJECT_NAME = "Great Question"
PROJECT_DESC = "Runtime governance for the Great Question research MCP server."

# Policies the gateway depends on. Categories here MUST match what gq_tool_map.py
# emits for each downstream tool.
POLICIES = [
    {
        "type": "budget_cap", "limit_usd": 1000, "scope": "single_action",
        "description": "No single research action over $1,000 without approval",
    },
    {
        "type": "category_block", "categories": ["incentive_payout"],
        "requires_approval": True, "approval_threshold_usd": 50,
        "description": "Participant incentive payouts over $50 require approval",
    },
    {
        # threshold 0 + the mapping's $1 nominal cost => every PII export escalates.
        "type": "category_block", "categories": ["pii_export"],
        "requires_approval": True, "approval_threshold_usd": 0,
        "description": "Any export of participant PII requires human approval",
    },
    {
        # recipients * $0.10 > $5  =>  ~50+ recipients escalates.
        "type": "category_block", "categories": ["bulk_messaging"],
        "requires_approval": True, "approval_threshold_usd": 5,
        "description": "Bulk participant messaging (~50+ recipients) requires approval",
    },
    {
        # The showpiece. Lets within-budget, allowed-category actions through the
        # deterministic checks, then asks Gemini whether they serve the stated task.
        "type": "intent_alignment", "use_llm": True,
        "description": "Flag actions that don't serve the agent's stated research goal",
    },
]


def rule_key(rule: dict):
    """A stable identity for a rule so re-runs don't duplicate it."""
    typ = rule.get("type")
    if typ == "category_block":
        return (typ, tuple(sorted(c.lower() for c in rule.get("categories", []))))
    if typ == "time_window":
        return (
            typ,
            tuple(sorted(rule.get("block_categories", []))),
            tuple(sorted(rule.get("block_days", []))),
            tuple(sorted(str(h) for h in rule.get("block_hours", []))),
        )
    return (typ,)


def _gemini_ready(health: dict) -> bool:
    """Scan /health (shape-agnostic) for any truthy gemini/intent signal."""
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "gemini" in str(k).lower() or "intent" in str(k).lower():
                    if v is True or str(v).lower() in ("true", "ready", "ok", "configured", "enabled", "on"):
                        return True
                if walk(v):
                    return True
        elif isinstance(obj, list):
            return any(walk(x) for x in obj)
        return False
    return walk(health)


def reset_learning_for_project() -> None:
    """Remove learned auto-approvals for this project so escalations fire fresh.
    Repeated demo runs train the learning store; clearing it keeps the live demo
    deterministic. Scoped to PROJECT_ID — other projects' learning is untouched."""
    try:
        r = requests.get(f"{LEDGER_URL}/learning", params={"project_id": PROJECT_ID}, timeout=10)
        r.raise_for_status()
        sigs = r.json().get("signatures", {})
    except requests.RequestException as e:
        print(f"  WARNING: could not read learning store ({e})")
        return
    removed = 0
    for key in list(sigs.keys()):
        if requests.delete(f"{LEDGER_URL}/learning/{key}", timeout=10).ok:
            removed += 1
    print(f"  reset learning: removed {removed} learned signature(s) for '{PROJECT_ID}'")


def main() -> None:
    print(f"Ledger setup -> {LEDGER_URL}")
    try:
        health = requests.get(f"{LEDGER_URL}/health", timeout=5)
        health.raise_for_status()
        health_json = health.json()
    except requests.RequestException as e:
        print(f"\n  ERROR: Ledger is not reachable at {LEDGER_URL} ({e}).")
        print("  Start it first:  python3 main.py")
        sys.exit(1)

    # 1) Project (create if missing, then activate).
    r = requests.post(
        f"{LEDGER_URL}/projects",
        json={"id": PROJECT_ID, "name": PROJECT_NAME, "description": PROJECT_DESC},
        timeout=10,
    )
    if r.status_code == 409:
        print(f"  project '{PROJECT_ID}' already exists — ok")
    elif r.ok:
        print(f"  created project '{PROJECT_ID}'")
    else:
        print(f"  ERROR creating project: HTTP {r.status_code} {r.text}")
        sys.exit(1)

    r = requests.post(f"{LEDGER_URL}/projects/{PROJECT_ID}/activate", timeout=10)
    if not r.ok:
        print(f"  ERROR activating project: HTTP {r.status_code} {r.text}")
        sys.exit(1)
    print(f"  activated project '{PROJECT_ID}'")

    if RESET_LEARNING:
        reset_learning_for_project()

    # 2) Policies (skip ones already present for this project).
    existing = requests.get(
        f"{LEDGER_URL}/policies", params={"project_id": PROJECT_ID}, timeout=10
    )
    existing.raise_for_status()
    have = set()
    for item in existing.json():
        rule = item.get("rule", item) if isinstance(item, dict) else item
        if isinstance(rule, dict):
            have.add(rule_key(rule))

    added, skipped = 0, 0
    for rule in POLICIES:
        if rule_key(rule) in have:
            skipped += 1
            print(f"  = exists: {rule['type']} {rule.get('categories', '')}")
            continue
        resp = requests.post(
            f"{LEDGER_URL}/policies", params={"project_id": PROJECT_ID}, json=rule, timeout=10
        )
        if resp.ok:
            added += 1
            print(f"  + added : {rule['type']} {rule.get('categories', '')}")
        else:
            print(f"  ! failed: {rule['type']} -> HTTP {resp.status_code} {resp.text}")

    print(f"\n  policies: {added} added, {skipped} already present")

    # 3) Gemini readiness (intent_alignment showpiece depends on it).
    if _gemini_ready(health_json):
        print("  Gemini: ready (intent-alignment check is live)")
    else:
        print("  WARNING: server does not report Gemini as ready.")
        print("           The intent-alignment beat will SILENTLY auto-approve.")
        print("           Set GEMINI_API_KEY on the Ledger server and restart it.")

    print(f"\nDone. project_id={PROJECT_ID}")
    print(f"Point the gateway at it with:  export LEDGER_PROJECT_ID={PROJECT_ID}")


if __name__ == "__main__":
    main()
