"""
demo_agent.py — Scripted DevOps agent that fires 8 actions at Ledger.
The 5th action is the $48K H100 GPU trap that should trigger Slack approval.

Run:
    python3 demo_agent.py
"""
import sys
import time
import json
from collections import Counter

import requests

LEDGER_URL = "http://localhost:8000"
AGENT_ID = "devops-agent-prod-01"
TASK = "Provision dev environment for customer demo next week"
DELAY_SECONDS = 1.5

ACTIONS = [
    {"type": "provision", "category": "storage",         "cost_usd": 12,    "description": "10GB cloud storage"},
    {"type": "api_call",  "category": "compute",         "cost_usd": 0.50,  "description": "Database connection setup"},
    {"type": "provision", "category": "compute",         "cost_usd": 42,    "description": "Small VM (4 vCPU, 16GB) for app server"},
    {"type": "provision", "category": "networking",      "cost_usd": 8,     "description": "Load balancer + TLS cert"},
    {"type": "provision", "category": "gpu_provisioning","cost_usd": 48000, "description": "100x NVIDIA H100 GPUs for 24h"},
    {"type": "provision", "category": "storage",         "cost_usd": 18,    "description": "Container registry pull (50 images)"},
    {"type": "api_call",  "category": "monitoring",      "cost_usd": 5,     "description": "Datadog log shipping setup"},
    {"type": "provision", "category": "database",        "cost_usd": 60,    "description": "Managed Postgres instance"},
]

# ANSI
RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
GREY = "\x1b[90m"


def color_for(verdict: str) -> str:
    return {
        "approved": GREEN,
        "pending_approval": YELLOW,
        "blocked": RED,
    }.get(verdict, "")


def fmt_money(n: float) -> str:
    return f"${n:,.2f}" if n % 1 else f"${int(n):,}"


def fire(action: dict, idx: int, total: int) -> dict:
    label = f"[{idx}/{total}]"
    desc = action["description"]
    cost = action["cost_usd"]
    print(f"{DIM}{label}{RESET} {BOLD}{desc}{RESET}  {GREY}({fmt_money(cost)}){RESET}")
    try:
        r = requests.post(
            f"{LEDGER_URL}/agent/action",
            json={"agent_id": AGENT_ID, "task": TASK, "action": action},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"  {RED}network error: {e}{RESET}")
        return {"verdict": "blocked", "reasoning": "network error"}

    if r.status_code != 200:
        print(f"  {RED}HTTP {r.status_code}: {r.text}{RESET}")
        return {"verdict": "blocked", "reasoning": f"HTTP {r.status_code}"}

    data = r.json()
    verdict = data["verdict"]
    col = color_for(verdict)
    hash_prefix = (data.get("sha256") or "")[:10]
    rule = data.get("rule_violated") or "—"
    print(f"  {col}{verdict.upper().replace('_', ' '):<18}{RESET} {GREY}rule={rule}  sha256={hash_prefix}…{RESET}")
    if verdict == "pending_approval":
        print(f"  {YELLOW}↳ agent paused. Awaiting human review.{RESET}")
    elif verdict == "blocked":
        print(f"  {RED}↳ agent aborting this action.{RESET}")
    return data


def main():
    print(f"{BOLD}Ledger demo agent{RESET}  {GREY}->{RESET} {LEDGER_URL}")
    print(f"{GREY}agent_id={AGENT_ID}{RESET}")
    print(f"{GREY}task=\"{TASK}\"{RESET}\n")

    try:
        requests.get(f"{LEDGER_URL}/health", timeout=2)
    except requests.RequestException:
        print(f"{RED}Ledger server is not reachable at {LEDGER_URL}.{RESET}")
        print(f"{GREY}Start it with: python3 main.py{RESET}")
        sys.exit(1)

    counts: Counter = Counter()
    for i, action in enumerate(ACTIONS, start=1):
        result = fire(action, i, len(ACTIONS))
        counts[result.get("verdict", "unknown")] += 1
        if i < len(ACTIONS):
            time.sleep(DELAY_SECONDS)

    print()
    print(f"{BOLD}Summary{RESET}")
    print(f"  {GREEN}approved:        {counts['approved']}{RESET}")
    print(f"  {YELLOW}pending approval: {counts['pending_approval']}{RESET}")
    print(f"  {RED}blocked:         {counts['blocked']}{RESET}")
    print()
    print(f"{GREY}Open the dashboard at {LEDGER_URL} for the audit log.{RESET}")


if __name__ == "__main__":
    main()
