# Ledger — Claude Code Project Context

You are helping build **Ledger**, a runtime policy and evidence layer for
autonomous AI agent actions. This is a hackathon project for the AI & Big Data
Expo (May 18–19, 2026 in San Jose). Submission deadline is 5:00 PM May 18.
Live on-stage pitch is May 19 at 12:45 PM.

## What it is, in one sentence

A FastAPI proxy that sits between AI agents and the world. Agents POST intended
actions (spend money, provision infra, etc.). Ledger evaluates against
enterprise-defined policy, escalates edge cases to Slack for human approval,
and logs every decision with a SHA-256 hash for audit-grade traceability.

## Read these files before doing anything

1. `PRD.md` — full product requirements, demo flow, architecture
2. `BUILD_PLAN.md` — hour-by-hour build sequence with exact deliverables
3. `DEMO_SCRIPT.md` — the 90-second pitch script we are optimizing the build for

The PRD is the source of truth for scope. **Do not add features outside the P0
scope.** Stretch features are explicitly marked P1.

## Stack (locked)

- Python 3.11+, FastAPI, Uvicorn
- SQLite (stdlib `sqlite3`, no ORM)
- Gemini 2.0 Flash via `google-generativeai` SDK
- Single HTML file frontend, vanilla JS, Tailwind CDN, dark theme
- Slack Incoming Webhook (P0) + Block Kit cards
- Slack Interactive Components via ngrok (P1, only if time)

## What's already built

The current `main.py` has:
- FastAPI scaffold
- SQLite init with `actions` and `policies` tables
- `/agent/action` endpoint with deterministic policy engine
- `/audit` endpoint
- `/policies` endpoint
- SHA-256 hashing per action
- Default seed policies

Verified working with curl tests.

## What needs building (in order)

1. **Gemini intent-alignment check** in policy engine
2. **Slack webhook integration** — pretty Block Kit card on `pending_approval`
3. **`/approve/{action_id}` endpoint** — mark pending action as approved/denied
4. **Dashboard HTML** — single page, dark theme, live action stream + audit log + policy view + Policy Copilot text box
5. **Policy Copilot** — `POST /policies/from_text` endpoint, Gemini generates JSON rule from natural language
6. **Demo agent script** — `demo_agent.py` that fires 8 scripted actions including the $48K GPU trap

## Coding style rules

- Keep it simple. Hackathon means demo-clean code, not production-clean code.
- Single file where possible. `main.py` can grow; do not pre-emptively split into modules unless it exceeds ~600 lines.
- No ORMs. Use raw `sqlite3` with `with db() as conn:` pattern that already exists.
- No frontend frameworks. Vanilla JS + fetch + Tailwind classes.
- No build steps. Everything runs with `python main.py`.
- Frontend: dark background (#0a0a0a-ish), green accent for approved, red for blocked, yellow for pending, monospace font. Steal LoxeAI's aesthetic — looks like serious infrastructure.
- All timestamps are ISO8601 UTC.
- All IDs are UUID4 strings.
- All hashes are SHA-256 over canonical JSON.

## Demo-driving requirements

The build exists to serve the demo. When in doubt, prioritize:

1. **Visual clarity** — judges should understand what's happening in 1 second of looking at the dashboard
2. **Demo reliability** — would rather have 6 polished features than 10 flaky ones
3. **Story coherence** — every feature should map to a beat in `DEMO_SCRIPT.md`

## What NOT to do

- Don't add authentication
- Don't add a database migration system
- Don't add tests beyond a single happy-path script
- Don't refactor the existing working code
- Don't add features not in the PRD P0
- Don't use frameworks (React, Vue, etc.) for the dashboard
- Don't add fancy animations that could break during demo

## Environment variables

```
GEMINI_API_KEY    — Google AI Studio key (required for intent check + copilot)
SLACK_WEBHOOK_URL — Slack Incoming Webhook URL (required for approval pings)
LEDGER_DB         — SQLite path, defaults to ./ledger.db
SLACK_SIGNING_SECRET — Only for P1 interactive buttons
```

## Run

```bash
pip3 install -r requirements.txt
export GEMINI_API_KEY=...
export SLACK_WEBHOOK_URL=...
python3 main.py
```

Server runs on `http://localhost:8000`.

## Time check before any feature

Before starting any new feature, ask: "if this takes 2× as long as expected,
do we still demo successfully?" If no, simplify or skip.
