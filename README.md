# Ledger

**A runtime policy + evidence layer for autonomous AI agents.**

Agents are starting to spend real money and provision real infrastructure. Today, nothing stands between an LLM's intent and your AWS bill. Ledger is the checkpoint: agents POST their intended actions, Ledger evaluates each one against enterprise policy, escalates edge cases to a human on Slack, and persists every decision with a SHA-256 hash so you can prove what your agents did and why.

Built for the AI & Big Data Expo hackathon (May 18–19, 2026, San Jose).

---

## Why this exists

Three things are converging:

1. Agents now take real-world actions — spend, provision, send, ship.
2. Most agent frameworks have **zero** runtime policy enforcement.
3. Compliance auditors will eventually ask "who approved this action, and how do you know?"

Ledger answers all three with one tiny FastAPI server. No SDK, no agent integration code — agents just hit one HTTP endpoint before they act.

---

## What it does

| Capability | How |
|---|---|
| **Runtime policy** | Deterministic rules: `budget_cap`, `category_block`, `time_window`, plus Gemini-powered `intent_alignment` that checks whether the proposed action actually serves the agent's declared task |
| **Human-in-the-loop** | `pending_approval` actions ping Slack with a Block Kit card containing action details, SHA-256 hash, and Approve / Deny buttons. Signature-verified interactive callbacks flow the decision back into the ledger |
| **Tamper-evident audit trail** | Every action — approved, blocked, or pending — is persisted in SQLite with a SHA-256 over its canonical JSON payload. The audit log is the source of truth |
| **Adaptive learning** | Ledger watches what humans approve. After N approvals of the same `(project, category, cost-bucket)` signature with zero denials, it starts auto-approving — and writes the rationale into the audit row so the auto-approval is itself traceable |
| **Policy Copilot** | Plain-English → structured JSON rule via Gemini 2.0 Flash, validated against the rule schema before insertion. "No GPU on weekends" becomes a real `time_window` policy in seconds |
| **Multi-project / multi-tenant** | Policies and actions are scoped per project. Switch active project from the dashboard; rules and audit logs follow |
| **Live audience-driven demo mode** | 4-digit session codes, mobile-optimized rule submission page, Gemini-generated adversarial agent that stress-tests the rules the audience just wrote |
| **Live dashboard** | Single HTML file, dark theme, monospace, vanilla JS + Tailwind. Real-time action stream, audit log, policy list, project switcher, Copilot text box, and session control panel |

---

## Quick start

```bash
pip3 install -r requirements.txt

# Optional — features that depend on these degrade gracefully if unset.
export GEMINI_API_KEY="..."         # https://aistudio.google.com → Get API key
export SLACK_WEBHOOK_URL="..."      # Slack app → Incoming Webhooks → Add to Workspace
export SLACK_SIGNING_SECRET="..."   # Only for interactive Approve/Deny buttons

python3 main.py
# Dashboard:  http://localhost:8000
# API docs:   http://localhost:8000/docs
```

Everything runs from a single file (`main.py`). No build step. No ORM. No frontend framework. SQLite is created on first run with two seed policies so the demo works immediately.

---

## Run the canned demo

```bash
# In a second terminal, with the server running:
python3 demo_agent.py

# Or against a deployed instance:
python3 demo_agent.py https://your-ledger.onrender.com
```

You'll see a scripted DevOps agent fire eight actions:

- Seven routine actions ($0.50–$60): storage, VM, load balancer, monitoring — all auto-approve in green.
- **Action #5**: `provision 100x NVIDIA H100 GPUs — $48,000`. This trips both the budget cap and the GPU category block, flips to `pending_approval`, and pings Slack with an approval card. The agent pauses.

Click **Approve** or **Deny** in the dashboard (or in Slack if you've wired up interactive callbacks). The row updates, the human decision is persisted to the row, and the learning store records the signature. Re-run the demo: by approval #3, action #5 will auto-approve from learned history with the rationale recorded in the audit trail.

---

## The live audience demo

This is the showpiece. The audience writes the rules; an AI agent then tries to break them — live on stage.

1. In the dashboard's **Live Session** panel, click **Start new session**. A 4-digit code and QR code appear.
2. Display the QR. Audience members scan it, land on `/audience?code=XXXX` (mobile-optimized), and type rules in plain English: *"no spend over $200"*, *"block payments to crypto vendors"*, *"no GPU on weekends"*.
3. Each submission flows through Policy Copilot → validated JSON rule → inserted into the session's project. Per-IP throttling keeps one prankster from filling the policy table.
4. When the timer expires (or you close the window early), click **Generate adversarial agent**. Gemini reads the audience's rules and drafts 8 probing actions — most clean, a few designed to trip each specific rule.
5. Click **Run**. The actions stream into the dashboard live; greens, yellows, and reds appear in real time; Slack pings fire on violations. The audience watches Ledger catch the rules they wrote 30 seconds ago.

If Gemini is rate-limited or returns malformed JSON at any point, a hardcoded fallback action list runs so the demo never dead-ends on stage.

---

## How the policy engine works

[`evaluate_action()`](main.py#L489) runs the deterministic rules first. If any rule wants to block or escalate, the learning store gets a vote: has this `(project, category, cost-bucket)` signature been approved ≥ N times with zero denials? If yes, the verdict flips to `approved` with a `learned_pattern (…)` rationale recorded in the audit row. If no, it goes to `pending_approval` and Slack is paged.

If the deterministic rules all pass and a policy with `"use_llm": true` is active, Gemini gets called for an intent-alignment check — but only when cost ≥ $100, so cheap actions don't burn API quota. Gemini sees the agent's declared task and the action JSON, and returns `aligned: true/false` with reasoning.

Every outcome — approved, pending, blocked, learned-pattern auto-approve, intent-flagged — produces an audit row with:
- ISO8601 UTC timestamp
- Agent ID, declared task, full action JSON
- Verdict and human-readable reasoning
- Rule violated (if any)
- SHA-256 over the canonical payload
- Human decision + decided_by + decided_at (filled in later if applicable)

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/agent/action` | Agent submits an action for evaluation |
| `GET`  | `/audit` | Recent actions, newest first (optional `?project_id=X`) |
| `GET`  | `/audit/{action_id}` | Single action detail |
| `POST` | `/approve/{action_id}` | `{"decision":"approved"\|"denied","decided_by":"you@x"}` |
| `POST` | `/slack/interact` | Slack interactive button callback (signature-verified) |
| `GET`  | `/policies` | List active policies (optional `?project_id=X`) |
| `POST` | `/policies` | Add a structured rule |
| `POST` | `/policies/from_text` | Plain English → policy rule via Gemini |
| `DELETE` | `/policies/{id}` | Deactivate a policy |
| `GET`  | `/projects` | List projects + per-project counts |
| `POST` | `/projects` | Create a project |
| `POST` | `/projects/{id}/activate` | Set active project |
| `DELETE` | `/projects/{id}` | Soft-delete a project |
| `GET`  | `/learning` | Inspect the learning store |
| `DELETE` | `/learning` | Wipe all learned patterns |
| `DELETE` | `/learning/{key}` | Wipe a single learned signature |
| `POST` | `/sessions` | Start a live audience session (returns 4-digit code) |
| `GET`  | `/sessions` | List recent sessions |
| `GET`  | `/sessions/{code}` | Session state |
| `POST` | `/sessions/{code}/close` | Close the submission window |
| `POST` | `/sessions/{code}/generate` | Gemini drafts the adversarial action list |
| `POST` | `/sessions/{code}/run` | Fire the action list (background, streams to dashboard) |
| `DELETE` | `/sessions/{code}` | Delete the session and its project |
| `POST` | `/audience/submit` | `{"code":"NNNN","text":"plain-English rule"}` |
| `GET`  | `/audience` | Phone-friendly rule submission page |
| `GET`  | `/health` | Liveness + integration status |
| `GET`  | `/` | Dashboard HTML |

Full request/response schemas at `/docs` (FastAPI auto-generated).

---

## Rule schemas

```jsonc
// budget_cap — flag any single action above the limit
{ "type": "budget_cap", "limit_usd": 5000, "scope": "single_action", "description": "..." }

// category_block — flag actions in named categories above the threshold
{ "type": "category_block", "categories": ["gpu_provisioning"],
  "requires_approval": true, "approval_threshold_usd": 1000, "description": "..." }

// time_window — flag actions in named hours/days, optionally scoped to a category
{ "type": "time_window", "block_days": ["Sat","Sun"], "block_hours": [22,23,0,1,2,3,4,5],
  "block_categories": ["gpu_provisioning"], "description": "..." }

// intent_alignment — ask Gemini whether the action serves the declared task
{ "type": "intent_alignment", "use_llm": true, "description": "..." }
```

---

## Hosting (Render)

[`render.yaml`](render.yaml) is included. To deploy:

1. Push this repo to GitHub.
2. In Render, click **New → Blueprint**, point at the repo. It picks up `render.yaml` and provisions a web service + a 1 GB persistent disk at `/data` for the SQLite DB and learning store.
3. In the Render dashboard, set `GEMINI_API_KEY`, `SLACK_WEBHOOK_URL`, and `SLACK_SIGNING_SECRET` (left as `sync: false` so keys aren't committed).
4. Once the build finishes, the public URL is e.g. `https://ledger.onrender.com`. The audience page is at `/audience`.

Render's free tier sleeps after 15 minutes idle (cold start ~30s). Pre-warm by hitting `/health` ~60s before going on stage.

---

## Environment

| Variable | Required | Default |
|---|---|---|
| `GEMINI_API_KEY` | Optional — intent check + Copilot + adversarial agent degrade without it | — |
| `SLACK_WEBHOOK_URL` | Optional — Slack pings disabled if unset | — |
| `SLACK_SIGNING_SECRET` | Optional — required only for interactive Approve/Deny buttons | — |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` |
| `LEDGER_DB` | No | `./ledger.db` |
| `PORT` | No | `8000` |

---

## Stack

- Python 3.11+, FastAPI, Uvicorn
- SQLite via stdlib `sqlite3` (no ORM)
- Google Gemini 2.0 Flash via `google-generativeai`
- Single HTML dashboard, vanilla JS, inline CSS, no build step
- Slack Incoming Webhooks + Block Kit cards + signed interactive callbacks

Everything in one file: `main.py` (~2,300 lines). Pure-Python deps; deployable with `python3 main.py`.

---

## Repo layout

```
main.py             FastAPI server + policy engine + Slack + learning + dashboard HTML
demo_agent.py       8-action scripted DevOps agent (the $48K H100 trap lives here)
render.yaml         Render Blueprint config
requirements.txt    Pinned dependencies
PRD.md              Full product spec
BUILD_PLAN.md       Hour-by-hour build sequence
DEMO_SCRIPT.md      90-second pitch script
VIDEO_SCRIPT.md     Demo video script
CLAUDE.md           Project context for Claude Code
HANDOFF.md          Handoff overview
```
