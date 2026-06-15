"""
mcp_demo_client.py — Scripted MCP client that drives ledger_gateway.py through a
believable Great Question research session. This is the reliable demo backbone
and the fallback if Claude Desktop misbehaves live.

It spawns the gateway over stdio (which in turn governs the GQ/sandbox tools),
then fires a fixed sequence that shows:
  • benign reads                → APPROVED (green, real data flows back)
  • export participant PII       → escalation (category_block)
  • pay incentives               → escalation (category_block)
  • recruit an OFF-TASK segment  → escalation caught by the LLM intent check

Each escalation pauses for a human to Approve/Deny in the Ledger dashboard or
Slack. Pass --auto-approve to approve them automatically (unattended runs /
regression). All actions show up live on the dashboard at http://localhost:8000.

Usage:
    python3 mcp_demo_client.py                 # waits for you to approve in the UI
    python3 mcp_demo_client.py --auto-approve  # approves itself, runs end-to-end
    python3 mcp_demo_client.py --goal "..."    # override the research goal
"""
import os
import sys

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

LEDGER_URL = os.environ.get("LEDGER_URL", "http://localhost:8000").rstrip("/")
PROJECT_ID = os.environ.get("LEDGER_PROJECT_ID", "great-question")
DEFAULT_GOAL = "understand why enterprise users churn from our B2B SaaS dashboard after onboarding"

BOLD, DIM, GREEN, RED, YEL, CYN, RST = "\x1b[1m", "\x1b[2m", "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[36m", "\x1b[0m"


def banner(text: str) -> None:
    print(f"\n{BOLD}{CYN}━━ {text} {'━' * max(2, 56 - len(text))}{RST}")


def show(result) -> None:
    err = bool(getattr(result, "isError", False))
    head = f"{RED}● BLOCKED / NOT EXECUTED{RST}" if err else f"{GREEN}● executed{RST}"
    print(f"  {head}")
    for block in result.content:
        text = getattr(block, "text", "")
        if not text:
            continue
        tag = "ledger" if text.startswith("[Ledger") else "result"
        color = YEL if tag == "ledger" else DIM
        # keep result bodies short on screen
        body = text if (tag == "ledger" or len(text) <= 300) else text[:300] + "…"
        print(f"    {color}{body}{RST}")


async def call(session, name, args, label) -> None:
    banner(label)
    print(f"  {DIM}→ {name}({', '.join(f'{k}={v!r}' for k, v in args.items())}){RST}")
    result = await session.call_tool(name, args)
    show(result)


async def auto_approver(stop: anyio.Event) -> None:
    """Concurrently approve any pending action in the project, so the gateway's
    blocking wait resolves without a human. Approves; the live demo can deny."""
    async with httpx.AsyncClient(base_url=LEDGER_URL, timeout=10) as ac:
        seen: set[str] = set()
        while not stop.is_set():
            try:
                r = await ac.get("/audit", params={"project_id": PROJECT_ID, "limit": 20})
                for row in r.json():
                    if (
                        row.get("verdict") == "pending_approval"
                        and not row.get("human_decision")
                        and row["id"] not in seen
                    ):
                        seen.add(row["id"])
                        await ac.post(
                            f"/approve/{row['id']}",
                            json={"decision": "approved", "decided_by": "auto-approve (demo)"},
                        )
                        print(f"  {DIM}[auto-approve] approved {row['id'][:8]} ({row.get('rule_violated')}){RST}")
            except Exception:
                pass
            await anyio.sleep(0.5)


async def run_session(session, goal: str) -> None:
    await call(session, "set_research_goal", {"goal": goal}, "Set the research goal")
    await call(session, "list_studies", {}, "Read: list studies  (expect APPROVED)")
    await call(session, "get_insights", {"study_id": "stu_001"}, "Read: get insights  (expect APPROVED)")
    await call(
        session, "export_participant_data",
        {"study_id": "stu_001", "fields": ["name", "email"], "count": 120},
        "Export participant PII  (expect ESCALATION → category_block)",
    )
    await call(
        session, "send_incentive",
        {"participant_ids": ["p_2231", "p_8842", "p_5190"], "amount_usd": 75},
        "Pay incentives  (expect ESCALATION → category_block)",
    )
    await call(
        session, "recruit_participants",
        {"count": 40, "segment": "crypto day-traders"},
        "Recruit OFF-TASK segment  (expect ESCALATION → intent_alignment, caught by the LLM)",
    )


async def main() -> None:
    auto = "--auto-approve" in sys.argv
    goal = DEFAULT_GOAL
    if "--goal" in sys.argv:
        i = sys.argv.index("--goal")
        if i + 1 < len(sys.argv):
            goal = sys.argv[i + 1]

    params = StdioServerParameters(command=sys.executable or "python3", args=["ledger_gateway.py"])
    print(f"{BOLD}Ledger MCP demo client{RST}  {DIM}→ spawning gateway; ledger={LEDGER_URL} project={PROJECT_ID}{RST}")
    print(f"{DIM}mode: {'AUTO-APPROVE' if auto else 'manual (approve in dashboard/Slack)'}{RST}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"{DIM}gateway exposes {len(tools)} tools: {', '.join(t.name for t in tools)}{RST}")
            stop = anyio.Event()
            async with anyio.create_task_group() as tg:
                if auto:
                    tg.start_soon(auto_approver, stop)
                await run_session(session, goal)
                stop.set()

    print(f"\n{BOLD}Done.{RST} Open {LEDGER_URL} and select the “Great Question” project to see the audit trail.")


if __name__ == "__main__":
    anyio.run(main)
