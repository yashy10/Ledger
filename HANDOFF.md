# Ledger Handoff Package

Everything you need to finish building Ledger with Claude Code.

## What's in this folder

| File | What it is | When to use |
|---|---|---|
| `HANDOFF.md` | This file | Read first |
| `CLAUDE.md` | Project context for Claude Code | Auto-read by Claude Code when you start it |
| `PRD.md` | Full product spec, demo, architecture | Reference doc, Claude Code reads it |
| `BUILD_PLAN.md` | Hour-by-hour task breakdown with hard cutoffs | Track progress, decide what to cut |
| `DEMO_SCRIPT.md` | The 90-second pitch with timing and fallbacks | Memorize before stage |
| `CLAUDE_CODE_PROMPTS.md` | 6 ordered prompts to paste into Claude Code | Use one at a time, in order |
| `main.py` | Working FastAPI scaffold (Hour 0 done) | Drop this in, don't modify |
| `requirements.txt` | Python deps | `pip3 install -r requirements.txt` |
| `README.md` | Quickstart for the existing code | Sanity check the setup |

## Setup (do this once, takes 5 min)

```bash
# 1. Make project folder
mkdir ~/ledger && cd ~/ledger

# 2. Drop all the handoff files in here (copy them from your downloads)

# 3. Install Python deps
pip3 install -r requirements.txt

# 4. Get your API keys ready
export GEMINI_API_KEY="..."         # https://aistudio.google.com → Get API key
export SLACK_WEBHOOK_URL="..."      # Slack app → Incoming Webhooks → Add to Workspace

# 5. Verify the Hour 0 build works
python3 main.py
# In another tab:
curl -X POST http://localhost:8000/agent/action \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"test","task":"test","action":{"type":"provision","category":"gpu_provisioning","cost_usd":48000,"description":"test"}}'
# Should return verdict: pending_approval
```

If that works, you're ready to hand off to Claude Code.

## Using Claude Code (the actual build)

```bash
cd ~/ledger
claude
```

First thing you say to Claude Code:

> "Read CLAUDE.md, PRD.md, BUILD_PLAN.md, and DEMO_SCRIPT.md before doing anything. Then summarize back to me what we're building and what's the next task."

Wait for it to confirm understanding. Then open `CLAUDE_CODE_PROMPTS.md` and paste prompts in order, one at a time. After each one:

1. Let Claude Code finish the task
2. Run the test the prompt specifies
3. Verify the behavior is right
4. Only then move to the next prompt

If anything breaks: tell Claude Code the exact error message and what you ran. Don't try to fix it yourself.

## Time discipline (critical)

| Time | Should be done |
|---|---|
| **1:30 PM** | Hour 1 (Gemini intent + Slack) ← Prompts 1 + 2 |
| **2:30 PM** | Hour 2 (Dashboard) ← Prompt 3 |
| **3:30 PM** | Hour 3 (Copilot + Demo agent) ← Prompts 4 + 5 |
| **4:00 PM** | Polish phase ← Prompt 6 |
| **4:30 PM** | Rehearsals only |
| **4:50 PM** | SUBMIT |

If at any cutoff you're behind, cut scope. The `BUILD_PLAN.md` has explicit "if behind, do this" decisions.

## What "done" looks like

A judge can:
1. Open your dashboard at `localhost:8000`
2. Watch you run `python3 demo_agent.py`
3. See 8 actions stream in, with the $48K GPU action lighting up red as pending
4. See a Slack notification appear on your phone with the approval request
5. Watch you approve or deny in either Slack or the dashboard
6. See the audit log update with a SHA-256 hash
7. Watch you type a policy in plain English and see it added to the active rules

If all 7 work, you have a winning demo.

## Submission

The hackathon submission form (lablab.ai workshop at 9:40 AM covered this — you should have the link). You'll need:

- Project name: **Ledger**
- Track: **Agent Security & AI Governance**
- One-liner: *"The policy and evidence layer for autonomous AI agent spend — runtime enforcement, human-in-the-loop, audit-grade traceability."*
- GitHub link (push your repo before submitting)
- Demo video (record a 90-second screen recording as backup — do this around 4:35 PM)

## If everything breaks

Worst case: even the Hour 0 build, deployed cleanly, with a screen recording showing the curl tests and DB hashes, is a credible submission for the track. The bar isn't "won the hackathon" — it's "submitted something coherent." But you can do much better than that.

Go build. 🚀
