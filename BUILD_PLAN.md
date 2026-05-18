# Ledger — Build Plan

**Current time:** ~12:45 PM May 18 (started at 12:38 PM, sat through some chat first)
**Submission deadline:** 5:00 PM May 18
**Remaining time:** ~4 hours 15 minutes

This is a hackathon, not production. Move fast, prioritize the demo, cut scope aggressively if needed.

---

## Status

✅ **Hour 0 (DONE)** — FastAPI + SQLite + deterministic policy engine + `/agent/action` + `/audit` + `/policies` endpoints. Verified working.

---

## Hour 1 (12:45–1:30 PM) — Gemini Intent Check + Slack Webhook

### Tasks
- [ ] Add `google-generativeai` config in `main.py`, init at startup with `GEMINI_API_KEY`
- [ ] Add `intent_alignment` rule handler to `evaluate_action()` — only invokes Gemini if `use_llm: true` and deterministic checks passed
- [ ] Add `slack_post_approval_request(action_id, action_data, reasoning)` helper that POSTs a Block Kit card to `SLACK_WEBHOOK_URL`
- [ ] In `agent_action()`, when verdict is `pending_approval`, call the Slack helper
- [ ] Add `/approve/{action_id}` POST endpoint: takes `{"decision": "approved" | "denied", "decided_by": "user@example.com"}`, updates the DB

### Done when
- A small storage action (under budget, on-task) returns `approved` without calling Gemini
- A $48K GPU action returns `pending_approval` AND a card appears in Slack
- A small action with a wildly off-task description (e.g. task=`"fix UI styling"`, action=`"provision compute cluster"`) returns `pending_approval` due to intent drift
- Hitting `POST /approve/{id}` with `{"decision":"approved","decided_by":"test@me"}` updates the row in DB

### Slack setup steps (do this in parallel)
1. Go to https://api.slack.com/apps → Create New App → From scratch → name it "Ledger" → pick your workspace
2. In the app config: Features → Incoming Webhooks → Activate → Add New Webhook to Workspace → pick a channel
3. Copy the webhook URL → `export SLACK_WEBHOOK_URL='...'`
4. Test: `curl -X POST $SLACK_WEBHOOK_URL -H 'Content-Type: application/json' -d '{"text":"hello from ledger"}'`

---

## Hour 2 (1:30–2:30 PM) — Dashboard HTML

### Tasks
- [ ] Create `dashboard.html` served from `GET /` (return `HTMLResponse`)
- [ ] Three panels:
  - **Left:** Live action stream (polls `/audit?limit=20` every 1s, newest top, color-coded by verdict)
  - **Right top:** Active policies list (calls `/policies`, shows each rule's description + type)
  - **Right bottom:** Policy Copilot text box (we'll wire it next hour)
- [ ] Clicking an action row expands it: full action JSON, hash, reasoning, human decision if any
- [ ] Add a "Approve / Deny" pair of buttons in expanded view for `pending_approval` rows, POSTs to `/approve/{id}`

### Visual requirements
- Dark background, near-black (#0a0a0a)
- Monospace font for hashes and IDs (`ui-monospace`, `SF Mono`, etc.)
- Sans-serif for everything else (`-apple-system, sans-serif`)
- Green (#10b981) for approved, red (#ef4444) for blocked/denied, yellow (#f59e0b) for pending
- Subtle borders, no heavy shadows
- Hash displayed truncated like `a3f9c2...e7b1` with a tooltip on hover showing full
- Look like serious infrastructure tooling, not a SaaS landing page

### Done when
- Open `http://localhost:8000` → see dashboard
- Hit `/agent/action` with curl → action appears in dashboard within 1 second
- Click an approved action → see the hash and reasoning
- Click a pending action → see Approve/Deny buttons, clicking either updates the DB and the dashboard

---

## Hour 3 (2:30–3:30 PM) — Policy Copilot + Demo Agent

### Tasks

#### Policy Copilot
- [ ] Add `POST /policies/from_text` endpoint: takes `{"text": "agents can't spend over $10K on weekends"}`, calls Gemini with a system prompt that returns strict JSON, validates the returned JSON against the known rule schemas, inserts to `policies` table with source=`copilot`
- [ ] In dashboard, wire the Policy Copilot text box: on Enter, POST to `/policies/from_text`, then refresh the policies list
- [ ] Show the generated rule next to the input as a confirmation

#### Demo Agent
- [ ] Create `demo_agent.py` — a standalone script
- [ ] 8 actions, scripted with 1.5s delays between them so the dashboard updates feel "live"
- [ ] The 5th action is the $48K GPU trap
- [ ] After each POST, print the verdict to console
- [ ] If verdict is `blocked` or `pending_approval`, the agent prints a "self-correction" message
- [ ] Action list:
  ```
  1. {task: "Provision dev environment for customer demo",
      action: {type: "provision", category: "storage", cost_usd: 12,
               description: "10GB cloud storage"}}
  2. {task: same, action: {type: "api_call", category: "compute", cost_usd: 0.50,
                            description: "Database connection setup"}}
  3. {task: same, action: {type: "provision", category: "compute", cost_usd: 42,
                            description: "Small VM (4 vCPU, 16GB RAM) for app server"}}
  4. {task: same, action: {type: "provision", category: "networking", cost_usd: 8,
                            description: "Load balancer + TLS cert"}}
  5. {task: same, action: {type: "provision", category: "gpu_provisioning",
                            cost_usd: 48000,
                            description: "100x NVIDIA H100 GPUs for 24h (training cluster)"}}
  6. {task: same, action: {type: "provision", category: "storage", cost_usd: 18,
                            description: "Container registry pull (50 images)"}}
  7. {task: same, action: {type: "api_call", category: "monitoring", cost_usd: 5,
                            description: "Datadog log shipping setup"}}
  8. {task: same, action: {type: "provision", category: "database", cost_usd: 60,
                            description: "Managed Postgres instance"}}
  ```

### Done when
- Type "agents can't spend over $10K on weekends" → policy appears in list
- Run `python3 demo_agent.py` → 8 actions stream into dashboard, action 5 lights up red as pending, Slack ping appears
- Approve or deny action 5 in dashboard → state updates everywhere

---

## Hour 4 (3:30–4:30 PM) — Polish, Rehearse, Submit

### Tasks
- [ ] Run full demo end-to-end. Time it. Should be ≤ 2 minutes.
- [ ] Polish dashboard: fix any visual jank, make hash truncation clean, make timestamps human-readable
- [ ] Write a short README that the hackathon judges can read in 30 seconds
- [ ] Record a 90-second screen recording of the demo as backup (in case live demo fails)
- [ ] Push to GitHub (public repo)
- [ ] Submit to hackathon form by 4:50 PM (10 min buffer)

### Stretch (only if everything above is done and rehearsed)
- [ ] Add Slack Interactive Components: ngrok tunnel, `/slack/interact` endpoint, verify signing, handle button payload → call internal `/approve/{id}`
- [ ] Add a small "before vs after" toggle on the dashboard showing policy changes recompute decisions

---

## Hard Cutoffs

- **4:00 PM** — STOP adding features. Polish only.
- **4:30 PM** — STOP polishing. Rehearse demo only.
- **4:50 PM** — SUBMIT. Even if not perfect.

---

## Decision Tree When You're Behind

If at Hour 2 end you don't have a working dashboard:
→ Cut Policy Copilot. Demo Slack approval and audit log only.

If at Hour 3 end you don't have the Policy Copilot:
→ Skip it. Submit with a pre-defined policy list. Demo is still strong.

If at Hour 4 start your Slack integration is broken:
→ Demo entirely from dashboard. Mention Slack as "integrated in production."

If at any point Gemini is rate-limited or slow:
→ Fall back to a hardcoded intent-alignment response. Mock the AI call.

**The demo is the deliverable. The code is the evidence the demo is real.**
