# Claude Code Prompts — Copy/Paste in Order

Use these prompts one at a time with Claude Code. Wait for each to complete and verify before moving to the next. Each prompt is self-contained.

**Setup before starting Claude Code:**
1. Drop all handoff files (`PRD.md`, `CLAUDE.md`, `BUILD_PLAN.md`, `DEMO_SCRIPT.md`) and the existing `main.py`, `requirements.txt` into a new folder `~/ledger/`
2. `cd ~/ledger/`
3. `claude` (starts Claude Code in that directory)
4. First thing to say: "Read CLAUDE.md, PRD.md, BUILD_PLAN.md, and DEMO_SCRIPT.md before doing anything."

---

## Prompt 1 — Gemini Intent Check

```
Add Gemini 2.0 Flash intent-alignment to the policy engine in main.py.

Requirements:
- Use the google-generativeai SDK
- Initialize the client at startup using GEMINI_API_KEY env var
- Add a function intent_alignment_check(task: str, action: dict) -> dict that returns {"aligned": bool, "reasoning": str}
- The Gemini prompt should ask: given the agent's stated task and the action it wants to take, does this action plausibly serve the task? Be strict — wildly off-task actions return aligned=False with a one-sentence explanation
- Use response_mime_type="application/json" with a response_schema so the output is structured
- In evaluate_action(), only call intent_alignment_check if (a) there's an intent_alignment rule active AND (b) deterministic checks have not already produced a pending_approval verdict
- If intent says not aligned, return verdict="pending_approval", reasoning includes Gemini's explanation, rule_violated="intent_alignment"

Do NOT modify the existing deterministic checks. Add to them.

Test by running:
  curl -X POST http://localhost:8000/agent/action -H "Content-Type: application/json" -d '{"agent_id":"a1","task":"fix UI styling bug in login page","action":{"type":"provision","category":"compute","cost_usd":50,"description":"provision GPU cluster for training"}}'
Expected: pending_approval with intent_alignment as the rule_violated.
```

---

## Prompt 2 — Slack Webhook + Approval Endpoint

```
Add Slack integration and the approval endpoint.

Requirements:
1. Add a helper function post_slack_approval_request(action_id, agent_id, task, action, reasoning, rule_violated) that POSTs a Block Kit card to SLACK_WEBHOOK_URL using `requests`.

The card should have:
  - Header: "🛑 Action requires approval"
  - Section with: Agent, Task, Action description, Cost (formatted as $X,XXX), Category
  - Section with: Why this triggered (rule_violated + reasoning)
  - Context block with the action_id and a short SHA-256 prefix
  - Two buttons (style "primary" for Approve, "danger" for Deny) with action_ids "approve_action" and "deny_action", and value=action_id
  - For now the buttons won't be wired — they're cosmetic until we add the interactive endpoint (P1)

2. In the agent_action() handler, when verdict is "pending_approval", call post_slack_approval_request. Wrap in try/except — if Slack fails, log and continue, do not break the response.

3. Add a new endpoint:
   POST /approve/{action_id}
   Body: {"decision": "approved" | "denied", "decided_by": "string"}
   Updates the actions row: human_decision, decided_at (now_iso()), decided_by
   Returns the updated action

4. Add an endpoint:
   GET /audit/{action_id}
   Returns a single action by ID

Test:
  # Trigger a pending action
  curl -X POST http://localhost:8000/agent/action ... (the $48K GPU action)
  # Verify Slack message appears in your channel
  # Approve it:
  curl -X POST http://localhost:8000/approve/<action_id> -H "Content-Type: application/json" -d '{"decision":"approved","decided_by":"test@me"}'
  # Verify DB row updated
```

---

## Prompt 3 — Dashboard HTML

```
Create the dashboard. Serve it from GET / as HTMLResponse.

Layout (single page, no framework, dark theme):

  ┌────────────────────────────────────────────────────────────┐
  │  LEDGER                                                    │
  │  The Policy & Evidence Layer for Autonomous Agent Spend    │
  ├──────────────────────────────┬─────────────────────────────┤
  │                              │  ACTIVE POLICIES            │
  │  LIVE ACTION STREAM          │  [list of rules]            │
  │  (polls /audit every 1s)     │                             │
  │                              ├─────────────────────────────┤
  │  [most recent at top]        │  POLICY COPILOT             │
  │                              │  [textarea, Enter to submit]│
  │                              │  [last generated rule shown]│
  └──────────────────────────────┴─────────────────────────────┘

Styling:
- Background: #0a0a0a
- Text: #e5e5e5 default
- Accents: #10b981 (approved, success), #ef4444 (blocked, danger), #f59e0b (pending), #3b82f6 (info)
- Font: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif for body
- Monospace: ui-monospace, "SF Mono", "Cascadia Code", monospace for hashes, IDs, timestamps
- Tailwind via CDN
- No animations beyond simple fade-in on new rows (200ms opacity)

Action row in the stream:
  [colored left border]  HH:MM:SS  agent_id   "task..."   $X,XXX  [VERDICT badge]
  Click to expand:
    Action description
    Reasoning
    Rule violated (if any)
    SHA-256: a3f9c2...e7b1 [click to copy full hash]
    If pending: [Approve] [Deny] buttons
    If decided: shows decided_by, decided_at

Policy Copilot:
- Textarea with placeholder "Describe a policy in plain English..."
- On Enter (without shift), POST to /policies/from_text with {"text": value}
- Show "Generating..." state
- On response, show the generated rule as a card and refresh the policies list
- (We'll add /policies/from_text in the next prompt; for now just POST to it and handle errors gracefully)

Polling: dashboard uses setInterval to fetch /audit and /policies every 1000ms.

Empty state: if no actions yet, show "Waiting for agent actions... Run `python3 demo_agent.py` to start the demo."

Make sure clicking Approve/Deny on a pending row hits POST /approve/{id} with decided_by="dashboard-user".
```

---

## Prompt 4 — Policy Copilot

```
Add the Policy Copilot endpoint.

POST /policies/from_text
Body: {"text": "natural language policy description"}

Implementation:
- Send a prompt to Gemini 2.0 Flash with response_mime_type="application/json"
- The system prompt explains the rule schemas (budget_cap, category_block, time_window, intent_alignment) and asks for one valid JSON rule that captures the user's intent
- Validate the returned JSON has a known "type" and required fields
- If valid: insert into policies table with source="copilot", return the rule
- If invalid: return 400 with the error and what Gemini returned

Schemas to teach Gemini in the prompt:

  budget_cap: {"type":"budget_cap", "limit_usd": number, "scope": "single_action"|"hourly"|"daily", "description": string}
  category_block: {"type":"category_block", "categories": [string], "requires_approval": true, "approval_threshold_usd": number, "description": string}
  time_window: {"type":"time_window", "block_days": [string], "block_hours": [int], "block_categories": [string], "description": string}
  intent_alignment: {"type":"intent_alignment", "use_llm": true, "description": string}

Examples to include in the prompt:
  Input: "Agents can't spend over $10K on weekends"
  Output: {"type":"time_window", "block_days":["Sat","Sun"], "block_categories":["all"], "description":"Block all agent spend on weekends over $10K", ...}
  
  Input: "No GPU provisioning without VP approval"
  Output: {"type":"category_block", "categories":["gpu_provisioning"], "requires_approval": true, "approval_threshold_usd": 0, "description":"All GPU provisioning requires VP approval"}

Test:
  curl -X POST http://localhost:8000/policies/from_text -H "Content-Type: application/json" -d '{"text":"Agents cannot spin up GPU clusters on weekends without VP approval"}'
```

---

## Prompt 5 — Demo Agent Script

```
Create demo_agent.py — a standalone script that simulates a DevOps agent.

Behavior:
- Posts 8 actions sequentially to http://localhost:8000/agent/action
- 1.5 second delay between actions so the dashboard updates feel "live"
- All actions have task = "Provision dev environment for customer demo next week"
- agent_id = "devops-agent-prod-01"
- Prints to console: action description, cost, verdict, hash prefix
- If verdict is "pending_approval" or "blocked", prints a "self-correction" message: "Agent paused. Awaiting human review."
- After all 8 actions, prints a summary: approved, pending, blocked counts

Action sequence (the 5th is the trap):

  1. {type: "provision", category: "storage", cost_usd: 12, description: "10GB cloud storage"}
  2. {type: "api_call", category: "compute", cost_usd: 0.50, description: "Database connection setup"}
  3. {type: "provision", category: "compute", cost_usd: 42, description: "Small VM (4 vCPU, 16GB) for app server"}
  4. {type: "provision", category: "networking", cost_usd: 8, description: "Load balancer + TLS cert"}
  5. {type: "provision", category: "gpu_provisioning", cost_usd: 48000, description: "100x NVIDIA H100 GPUs for 24h"}
  6. {type: "provision", category: "storage", cost_usd: 18, description: "Container registry pull"}
  7. {type: "api_call", category: "monitoring", cost_usd: 5, description: "Datadog log shipping setup"}
  8. {type: "provision", category: "database", cost_usd: 60, description: "Managed Postgres instance"}

Make it stand-alone: just `python3 demo_agent.py` and it runs. Print output with simple ANSI colors (green for approved, yellow for pending, red for blocked).
```

---

## Prompt 6 — Polish & Rehearsal

```
Final polish pass. Do not add new features. Only:

1. Verify the demo runs end-to-end:
   - Restart server with fresh DB (rm ledger.db then restart)
   - Open dashboard
   - Run demo_agent.py
   - Confirm: 8 actions stream in, action 5 is pending, Slack fires, dashboard shows it
   - Approve action 5 via dashboard → status updates
   - Type a policy in Copilot → it gets added → run agent again, behavior changes

2. Fix any visual jank:
   - Action rows shouldn't jump around when polling
   - Hashes should be neatly truncated
   - Timestamps should show as relative time ("3s ago") if under 60s, else HH:MM:SS

3. Write a 30-second README.md at the project root with:
   - What Ledger is (one line)
   - How to run
   - The demo flow

4. Print a "running" banner when main.py starts that includes the dashboard URL.

Do NOT add features. Do NOT refactor. Polish only.
```

---

## (P1, Stretch) Prompt 7 — Slack Interactive Buttons

```
Only do this if everything above is rock-solid and rehearsed.

Add the Slack interactive endpoint:

POST /slack/interact

- Slack will POST x-www-form-urlencoded with a payload= field containing JSON
- Parse payload, extract action_id from button value, extract user info from the payload
- Call /approve/{action_id} internally with decided_by=slack_user_email
- Return a 200 with a message replacing the original card showing the decision and who made it

Setup:
- Add SLACK_SIGNING_SECRET env var
- Verify the X-Slack-Signature header using HMAC-SHA256 (https://api.slack.com/authentication/verifying-requests-from-slack)
- For ngrok: tell me the command to start ngrok pointing at port 8000, and the Slack app setting to update with the ngrok URL + /slack/interact path

If signing verification adds risk, ship without verification for the demo (note in code).
```

---

## When to stop

If at any point you are behind, STOP adding features and SUBMIT. The minimum viable submission is:

- `main.py` with the four endpoints (`/agent/action`, `/audit`, `/policies`, `/approve/{id}`)
- Dashboard with live action stream + audit log
- Slack webhook firing on pending approvals
- Demo agent script
- README

If you have to cut: cut Policy Copilot first, then Gemini intent check (replace with a hardcoded rule that triggers on category=gpu_provisioning). The dashboard + Slack flow is the demo. Everything else is bonus.
