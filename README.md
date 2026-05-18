# Ledger — The Policy & Evidence Layer for Autonomous Agent Spend

A real-time checkpoint between AI agents and your money. Agents propose
actions, Ledger enforces policy, humans approve the edge cases, every decision
is logged with an audit-grade SHA-256 hash.

## What it does

| | |
|---|---|
| **Runtime policy** | Budget caps, category blocks, time windows, Gemini-powered intent alignment |
| **Human-in-the-loop** | Pending actions ping Slack with a Block Kit approval card |
| **Audit trail** | Every action stored in SQLite with a SHA-256 of its canonical payload |
| **Policy Copilot** | Plain-English → JSON policy rule via Gemini 2.0 Flash |
| **Live dashboard** | Dark-theme single-page UI: action stream, audit log, policy list, Copilot |

## Quick start

```bash
pip3 install -r requirements.txt

# Optional — features that depend on these will degrade gracefully if unset.
export GEMINI_API_KEY="..."         # https://aistudio.google.com → Get API key
export SLACK_WEBHOOK_URL="..."      # Slack app → Incoming Webhooks → Add to Workspace

python3 main.py
# Dashboard:  http://localhost:8000
# API docs:   http://localhost:8000/docs
```

## Run the demo

```bash
# In a second terminal, with the server running:
python3 demo_agent.py
```

You'll see eight actions stream into the dashboard. Action #5 (`100x NVIDIA
H100 GPUs — $48,000`) trips the budget cap, fires a Slack approval card, and
sits in `pending_approval`. Click **Approve** or **Deny** in the dashboard;
the row updates and the decision is persisted.

## Live audience-driven demo

Ledger has a built-in "live session" mode for on-stage demos where the audience writes the
governance rules themselves and an AI-generated agent then attacks those rules.

1. Click **Start new session** in the dashboard's *Live Session* panel. A 4-digit code +
   QR appear.
2. Display the URL + code on a slide. Audience visits `https://<host>/audience`, enters the
   code, and types a rule in plain English (e.g. *"no spend over $200"*). The Policy
   Copilot turns each submission into a structured rule scoped to the session's project.
3. After ~20s (or click **Close window**), click **Generate adversarial agent**. Gemini
   reads the submitted rules and drafts 8 probing actions — some clean, some designed to
   trip each rule.
4. Click **Run**. The actions stream into the dashboard; Slack pings fire on violations;
   the audience watches Ledger catch (or miss) rules they wrote 30 seconds ago.

If Gemini is rate-limited or returns malformed JSON, a hardcoded 8-action fallback
script runs so the demo never dead-ends.

## Hosting (Render)

`render.yaml` is included. To deploy:

1. Push this repo to GitHub.
2. In Render, click **New → Blueprint**, point at the repo. It picks up `render.yaml`
   and provisions a web service + a 1 GB persistent disk at `/data` (where the SQLite
   DB and `learning.json` live).
3. In the Render dashboard, set `GEMINI_API_KEY` and `SLACK_WEBHOOK_URL` (left as
   `sync: false` in `render.yaml` so the keys don't get committed).
4. Once the build finishes, the public URL is e.g. `https://ledger.onrender.com`.
   The audience page is at `/audience`.

Render's free tier sleeps after 15 minutes idle (cold start ~30s). Pre-warm by hitting
`/health` ~60s before going on stage.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/agent/action` | Agent submits an action for evaluation |
| `GET`  | `/audit` | Recent actions (newest first; optional `?project_id=X`) |
| `GET`  | `/audit/{action_id}` | Single action detail |
| `POST` | `/approve/{action_id}` | `{"decision":"approved"|"denied","decided_by":"you@x"}` |
| `GET`  | `/policies` | List active policies (optional `?project_id=X`) |
| `POST` | `/policies` | Add a structured rule |
| `POST` | `/policies/from_text` | Plain English → policy rule via Gemini |
| `DELETE` | `/policies/{id}` | Deactivate a policy |
| `GET`  | `/projects` | List projects + per-project counts |
| `POST` | `/projects` | Create a project |
| `POST` | `/projects/{id}/activate` | Set active project |
| `DELETE` | `/projects/{id}` | Soft-delete a project |
| `GET`  | `/learning` | Inspect the RL signature store |
| `DELETE` | `/learning` | Wipe all learned patterns |
| `POST` | `/sessions` | Start a live audience session (returns 4-digit code) |
| `GET`  | `/sessions/{code}` | Session state |
| `POST` | `/sessions/{code}/close` | Close the submission window |
| `POST` | `/sessions/{code}/generate` | Gemini drafts the adversarial action list |
| `POST` | `/sessions/{code}/run` | Fire the action list (background, ~10s) |
| `POST` | `/audience/submit` | `{"code":"NNNN","text":"plain-English rule"}` |
| `GET`  | `/audience` | Phone-friendly rule submission page |
| `GET`  | `/health` | Liveness + integration status |
| `GET`  | `/` | Dashboard HTML |

## Rule schemas

```jsonc
// budget_cap
{ "type": "budget_cap", "limit_usd": 5000, "scope": "single_action", "description": "..." }

// category_block
{ "type": "category_block", "categories": ["gpu_provisioning"],
  "requires_approval": true, "approval_threshold_usd": 1000, "description": "..." }

// time_window
{ "type": "time_window", "block_days": ["Sat","Sun"], "block_hours": [22,23,0,1,2,3,4,5],
  "block_categories": ["gpu_provisioning"], "description": "..." }

// intent_alignment (Gemini)
{ "type": "intent_alignment", "use_llm": true, "description": "..." }
```

## Environment

| Variable | Required | Default |
|---|---|---|
| `GEMINI_API_KEY` | Optional (intent check + Copilot degrade without it) | — |
| `SLACK_WEBHOOK_URL` | Optional (Slack pings disabled if unset) | — |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` |
| `LEDGER_DB` | No | `./ledger.db` |

## What's in this repo

```
main.py             FastAPI server, policy engine, Slack, dashboard
demo_agent.py       8-action scripted DevOps agent
requirements.txt    pinned deps
PRD.md              full product spec
BUILD_PLAN.md       hour-by-hour build sequence
DEMO_SCRIPT.md      90-second pitch script
CLAUDE.md           project context for Claude Code
HANDOFF.md          handoff overview
```
