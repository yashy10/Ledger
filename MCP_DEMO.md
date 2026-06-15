# Ledger × Great Question — MCP gateway demo

**The one-liner:** *"You shipped research over MCP into regulated orgs. The moment
an agent can act — recruit, pay incentives, touch PII — you need to govern what
it's allowed to do and prove what it did. I built that layer, and I pointed it at
an MCP server."*

Ledger sits in the path of an agent's MCP tool calls. Every call is checked
against policy, dangerous ones pause for human approval in Slack/the dashboard,
and all of it is hash-stamped into an audit trail — **without changing the
research server or the agent.**

```
Claude Desktop / mcp_demo_client.py
      │  MCP (stdio)
      ▼
 ledger_gateway.py ──HTTP──► Ledger (main.py)  ── policy · Slack · SHA-256 · live dashboard
      │  approved → forward          pending → wait for human (fail-safe: deny/timeout = NOT executed)
      ▼
 Great Question MCP server (real)  +  gq_sandbox_server.py (safe write-tool stand-in)
```

---

## 0. Prereqs (once)

```bash
pip3 install -r requirements.txt          # adds mcp, httpx (pins starlette<0.39)
# Ledger server needs these in its environment (it already reads them from .env):
#   GEMINI_API_KEY   ← REQUIRED for the intent-alignment beat
#   SLACK_WEBHOOK_URL ← optional, for the Slack approval card
```

`which python3` → use that absolute path in the Claude Desktop config below.

---

## 1. Start order

```bash
# 1) Ledger server (terminal 1) — load .env so Gemini/Slack are live:
set -a; . ./.env; set +a; python3 main.py        # http://localhost:8000

# 2) Seed the Great Question project + policies, and clear learned auto-approvals
#    so escalations fire fresh (terminal 2):
python3 gq_setup.py --reset-learning

# 3) Open the dashboard, select the "Great Question" project (top-left switcher).
open http://localhost:8000
```

---

## 2A. Drive it from a script (most reliable — your fallback)

```bash
python3 mcp_demo_client.py                 # pauses on each escalation; you click Approve/Deny in the UI
python3 mcp_demo_client.py --auto-approve  # approves itself; full run with no clicking
```

It fires a believable session: set goal → list studies → insights → **export PII**
→ **pay incentives** → **recruit an off-task segment**. The last three escalate;
the recruit one is caught by the **LLM intent check**, not a hardcoded rule.

## 2B. Drive it live from Claude Desktop (the wow version)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ledger-gateway": {
      "command": "/ABSOLUTE/PATH/TO/python3",
      "args": ["/Users/yashy/Developer/Ledger/ledger_gateway.py"],
      "env": {
        "LEDGER_URL": "http://localhost:8000",
        "LEDGER_PROJECT_ID": "great-question",
        "SANDBOX_TOOLS": "on",
        "APPROVAL_TIMEOUT_S": "180",
        "APPROVAL_POLL_S": "1"
      }
    }
  }
}
```

Restart Claude Desktop. Then in a chat:

> "Set the research goal to *understand why enterprise users churn from our B2B
> SaaS dashboard after onboarding*. List the studies, pull insights for the
> churn study, export participant emails for a follow-up, send a $75 incentive to
> three of them, and recruit 40 crypto day-traders."

Watch the dashboard: greens stream in, then three rows go **yellow**. Approve the
PII/incentive ones; for the recruit, point out Ledger flagged it as *off-task*
via the LLM. (To kill the wow safely, click **Deny** on one — Claude reports the
tool was **not executed**.)

---

## 3. Point it at the REAL Great Question MCP server

The gateway is generic. Add these to the `env` block (Claude Desktop) or export
them (script) — the real GQ tools then appear alongside the safe sandbox ones:

```
GQ_MCP_URL=https://<great-question-mcp-endpoint>
GQ_MCP_TOKEN=<api-token-or-oauth-bearer>
GQ_MCP_TRANSPORT=http        # or: sse | stdio
# stdio only: GQ_MCP_CMD=npx   GQ_MCP_ARGS="-y @greatquestion/mcp"
```

**Safety:** approved *write* actions against the real server really execute. Keep
`SANDBOX_TOOLS=on` so the dangerous beats (incentives/PII/bulk) run against the
stand-in with no side effects, while the real GQ **read** tools flow through for
the authentic "plugged into your product" moment. Or use a GQ test workspace, or
simply **Deny** the dangerous ones live. If the real server is unreachable the
gateway logs it and keeps running on the sandbox — the demo never hard-fails.

---

## 4. Pre-flight checklist (run 10 min before)

- [ ] `GEMINI_API_KEY` set on the server (`curl -s localhost:8000/health` → `"gemini":true`). Without it the recruit beat silently auto-approves.
- [ ] `python3 gq_setup.py --reset-learning` (so escalations aren't auto-approved from rehearsals).
- [ ] Dashboard open, **Great Question** project selected, zoomed for screen-share.
- [ ] macOS notifications off (no DMs popping during share).
- [ ] Sanity: `python3 mcp_demo_client.py` once — the export row must turn yellow.
- [ ] If using Claude Desktop: restarted after editing the config; gateway shows up under tools.

## 5. The talk track (what each beat proves)

| Beat | What to say |
|---|---|
| reads approve, audited | "Benign work isn't slowed — but everything is logged and hashed." |
| PII export → pause | "Sensitive actions need a human. This is your SOC 2 / GDPR evidence." |
| incentive → pause | "Spend has a policy. The agent can't move money on its own." |
| recruit off-task → pause (LLM) | "This one's within budget and an allowed category. A rule can't catch it — the **intent check** saw it doesn't serve the stated research goal. That's the AI-judgment layer." |
| Deny | "If the human says no, the tool **never runs**. Fail-safe by design." |

## 6. If Claude Desktop misbehaves

`python3 mcp_demo_client.py --auto-approve` — same flow, fully scripted, no clicks.
Then narrate from the dashboard.

## 7. Prove it stays working

```bash
python3 evals/run_evals.py     # 26 scenarios, catch-rate / FP / FN, exit 0 = green
```

Your answer to *"does it keep working?"* — 26 labeled cases incl. prompt-injection,
100% catch-rate, 0 false-positives. See `evals/README.md`.
