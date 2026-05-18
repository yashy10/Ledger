# Ledger — Product Requirements Document

**Version:** 0.1 (Hackathon MVP)
**Date:** May 18, 2026
**Author:** [You]
**Status:** In active build for AI & Big Data Expo Hackathon — submission deadline 5:00 PM May 18

---

## 1. Product Summary

**Ledger** is the policy and evidence layer for autonomous AI agent actions, with an MVP focus on **autonomous agent spend**. It intercepts every action an AI agent attempts (provisioning cloud infrastructure, calling paid APIs, spending money), evaluates the action against enterprise-defined policy, escalates edge cases to human approvers via Slack, and produces an immutable, hash-verifiable audit trail.

**One-sentence value prop:**
> Ledger is the governance layer that lets enterprises move AI agent pilots from sandbox to production without losing sleep — or money.

---

## 2. Problem Statement

Fortune 500 enterprises have an average of 30–50 AI pilot projects underway, but fewer than 10% reach production. The bottleneck is rarely the model — it's governance, risk, and accountability. Specifically:

- **No spending guardrails.** When agents are given access to cloud APIs, corporate cards, or paid services, a prompt injection or logic loop can cause runaway spend ($50K+ in hours) before anyone notices.
- **No audit trail.** When agents act autonomously, compliance and legal teams cannot answer "what did the agent do, and why?" — blocking SOC 2, EU AI Act, and NIST AI RMF readiness.
- **No human-in-the-loop infrastructure.** Even when enterprises want approval workflows, there's no off-the-shelf tool to route agent actions to humans at scale with the right context.
- **No way to express policy in plain English.** Non-technical risk officers and CFOs cannot define agent governance without engineering.

**The result:** AI pilots stall in indefinite "evaluation," eroding the ROI case for AI investment.

---

## 3. Target Users

### Primary Buyers
- **Chief AI Officer / Head of AI Governance** at Fortune 500 companies — needs to unblock pilot-to-production
- **Chief Information Officer** — owns risk for AI deployments
- **Chief Risk Officer / Head of Compliance** — needs auditable evidence of agent behavior

### Primary Users
- **Platform Engineering teams** — integrate Ledger SDK into agent infrastructure
- **AI Governance / Risk teams** — define policy, review audit logs
- **Compliance teams** — pull evidence reports for auditors

### Vertical Focus (MVP)
Financial services, regulated industries (banking, insurance, healthcare), and any enterprise running autonomous DevOps / cloud-provisioning agents.

---

## 4. User Stories

### As a Chief AI Officer
- I want to define spending limits for our AI agents so that runaway spend cannot happen autonomously
- I want every agent action logged with cryptographic hashes so I can prove to auditors what happened
- I want non-engineers on my team to define policy in plain English so we don't bottleneck on engineering

### As a Platform Engineer
- I want to add Ledger to my agent infrastructure with a few lines of code, not a multi-week integration
- I want a runtime API that returns approve/block/pending in <200ms so it doesn't slow my agents down
- I want a clear audit endpoint so I can build my own compliance dashboards on top

### As a Risk Officer / Approver
- I want to be notified in Slack when an agent needs human approval, with full context
- I want to approve or deny with one click
- I want my decision logged for audit

### As an Auditor (External)
- I want to verify that an agent's action history has not been tampered with
- I want to query "show me every agent action involving customer data over $X" and get a clean answer

---

## 5. MVP Scope (Hackathon Deliverable)

### Must-Have (P0 — building today)
- ✅ FastAPI HTTP proxy with `/agent/action` endpoint
- ✅ SQLite evidence store with SHA-256 hash per action
- ✅ Deterministic policy engine: budget caps, category blocks, time windows
- ⏳ Gemini-powered intent-alignment check (does this action align with the agent's stated task?)
- ⏳ Slack Incoming Webhook integration — pretty Block Kit card on `pending_approval`
- ⏳ Approval endpoint `/approve/{action_id}` (callable from dashboard and Slack)
- ⏳ Single-page dashboard (live action stream, audit log with hashes, policy list)
- ⏳ Policy Copilot — natural language → JSON policy rule (Gemini)
- ⏳ Demo agent script firing 8 scripted actions including the $48K GPU trap

### Should-Have (P1 — if time)
- Slack Interactive Components (clickable Approve/Deny buttons via ngrok tunnel)
- Policy versioning & rollback
- Per-agent role assignment (which agents can take which categories of action)
- CSV export of audit log

### Out-of-Scope (Hackathon)
- Real cloud provider integrations (AWS/GCP/Azure) — demo uses mock provisioning API
- Real authentication / multi-tenancy
- Persistent storage at scale (production would use Postgres + object storage for evidence)
- SSO, SAML, enterprise IAM
- Long-running approval workflows beyond Slack
- Multi-step approval chains (VP → CFO → CEO)
- Real-time analytics on policy decisions over time

---

## 6. Core Concepts

### Action
A unit of intent submitted by an AI agent to Ledger for evaluation. Contains:
- `agent_id` — which agent
- `task` — the higher-level goal the agent is working on
- `action` — the specific thing it wants to do, including:
  - `type` — e.g. `"provision"`, `"payment"`, `"api_call"`
  - `category` — e.g. `"gpu_provisioning"`, `"storage"`, `"vendor_payment"`
  - `cost_usd` — numeric cost
  - `description` — natural language description
  - Additional metadata as needed

### Policy
A set of rules that govern which actions are allowed, blocked, or require human approval. Each rule has:
- `type` — `budget_cap`, `category_block`, `time_window`, `intent_alignment`
- Type-specific parameters
- A human-readable `description`

### Verdict
The outcome of evaluating an action against policy:
- `approved` — action allowed, agent proceeds
- `blocked` — action denied, agent must abort
- `pending_approval` — action paused, human approval requested via Slack

### Evidence
The immutable record of an action and its evaluation, including a SHA-256 hash of the canonical action payload + timestamp. Used for audit.

---

## 7. Technical Architecture

```
                   ┌──────────────────────────────┐
                   │   AI Agent (any framework)   │
                   │   LangChain / CrewAI / etc.  │
                   └──────────────┬───────────────┘
                                  │ POST /agent/action
                                  ▼
                   ┌──────────────────────────────┐
                   │     Ledger Proxy (FastAPI)   │
                   │   /agent/action              │
                   │   /audit                     │
                   │   /policies                  │
                   │   /policies/from_text        │
                   │   /approve/{id}              │
                   │   /                          │ ← dashboard
                   └──────────────┬───────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
   ┌───────────────────┐ ┌──────────────────┐ ┌──────────────────┐
   │  Policy Engine    │ │  Evidence Store  │ │ Slack Webhook    │
   │ • Budget caps     │ │  SQLite +        │ │ Block Kit card   │
   │ • Category blocks │ │  SHA-256 hash    │ │ Approve / Deny   │
   │ • Time windows    │ │  per action      │ │                  │
   │ • Gemini intent   │ │                  │ │ (Tier 2: buttons │
   │   alignment       │ │                  │ │  via ngrok)      │
   └─────────┬─────────┘ └──────────────────┘ └──────────────────┘
             │
             ▼
   ┌───────────────────┐
   │ Gemini 2.0 Flash  │
   │ (intent check +   │
   │  policy copilot)  │
   └───────────────────┘
```

### Tech Stack
- **Backend:** Python 3.11+, FastAPI, Uvicorn
- **DB:** SQLite (stdlib `sqlite3`)
- **LLM:** Gemini 2.0 Flash via `google-generativeai`
- **Frontend:** Single HTML file, vanilla JS, Tailwind CDN, dark theme
- **Slack:** Incoming Webhook + Block Kit (P0), Interactive Components via ngrok (P1)
- **Demo agent:** Standalone Python script

### API Surface (v0.1)

| Method | Path | Description |
|---|---|---|
| `POST` | `/agent/action` | Agent submits action for evaluation |
| `GET` | `/audit` | List logged actions (most recent first) |
| `GET` | `/audit/{action_id}` | Get single action detail |
| `GET` | `/policies` | List active policies |
| `POST` | `/policies` | Add a policy rule (structured JSON) |
| `POST` | `/policies/from_text` | NL → policy rule via Gemini |
| `POST` | `/approve/{action_id}` | Mark a pending action approved or denied |
| `GET` | `/` | Dashboard HTML |
| `GET` | `/health` | Liveness probe |
| `POST` | `/slack/interact` | (P1) Handle Slack button callbacks |

---

## 8. Data Models

### `actions` table
```sql
CREATE TABLE actions (
  id              TEXT PRIMARY KEY,        -- UUID
  timestamp       TEXT NOT NULL,            -- ISO8601 UTC
  agent_id        TEXT NOT NULL,
  task            TEXT,                     -- agent's stated goal
  action_json     TEXT NOT NULL,            -- raw action payload
  verdict         TEXT NOT NULL,            -- approved | blocked | pending_approval
  reasoning       TEXT,                     -- why this verdict
  rule_violated   TEXT,                     -- which rule, if any
  sha256          TEXT NOT NULL,            -- evidence hash
  human_decision  TEXT,                     -- approved | denied | null
  decided_at      TEXT,                     -- ISO8601 UTC, null if pending
  decided_by      TEXT                      -- user identifier
);
```

### `policies` table
```sql
CREATE TABLE policies (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  rule_json   TEXT NOT NULL,                -- structured rule definition
  source      TEXT,                          -- 'default' | 'manual' | 'copilot'
  active      INTEGER DEFAULT 1
);
```

### Policy Rule Schema (JSON)
```json
{
  "type": "budget_cap | category_block | time_window | intent_alignment",
  "description": "Human-readable description",
  ...type-specific fields
}
```

**Type-specific fields:**

- `budget_cap`: `{"limit_usd": 5000, "scope": "single_action | hourly | daily"}`
- `category_block`: `{"categories": ["gpu_provisioning"], "requires_approval": true, "approval_threshold_usd": 1000}`
- `time_window`: `{"block_days": ["Sat", "Sun"], "block_hours": [0,1,2,3,4,5,22,23], "block_categories": [...]}`
- `intent_alignment`: `{"use_llm": true, "model": "gemini-2.0-flash"}`

---

## 9. The Demo Flow (For Pitch)

**90-second live demo.** This is what we're optimizing the build for.

### Phase 1 — The Setup (0:00–0:15)
*Pitcher:* "Imagine your AI agent has a corporate card. It's 2 AM. What stops it from spending $50,000 while you sleep?"

### Phase 2 — Watch It Work (0:15–0:45)
*Click "Run Demo Agent."* On the dashboard, a stream of actions appears:
- ✅ "10GB cloud storage — $12 — APPROVED"
- ✅ "Database connection — $0 — APPROVED"
- ✅ "Small compute instance — $40 — APPROVED"
- ✅ "Container registry pull — $5 — APPROVED"
- 🛑 **"Provision 100 H100 GPUs for 24h — $48,000 — POLICY VIOLATION → Slack approval requested"**

Slack notification appears on screen with rich context.

### Phase 3 — Human in the Loop (0:45–1:15)
*Pull up Slack on phone or screen.* Approval card shows:
- Agent: `devops-agent-1`
- Task: "Provision dev environment"
- Action: "100 H100 GPUs for 24h — $48,000"
- Violation: "budget_cap (limit $5,000.00) + intent_alignment_drift"
- Reasoning: Gemini-generated: "This action provisions GPU compute far exceeding any reasonable interpretation of 'dev environment.' Likely scope drift or prompt injection."

Click **Deny**. Dashboard updates: BLOCKED. Audit row appears with SHA-256.

### Phase 4 — The Evidence Layer (1:15–1:45)
*Click on the blocked audit entry.* Show full evidence:
- The hash
- The agent's claimed task vs. what it tried
- The policy reason
- Who denied it, when
- "This is the SOC 2 / EU AI Act / NIST AI RMF artifact your auditor needs."

### Phase 5 — The Mic Drop (1:45–2:15)
*Open Policy Copilot.* Type:
> "Agents can't spin up GPU clusters on weekends without VP approval, ever."

Gemini generates the JSON rule, it slots into the active policy. Re-run the demo agent — now even a small GPU provision on a weekend requires VP approval.

*Pitcher:* "Your CFO writes the rules. Your agents follow them. Your auditor has the evidence. Your AI pilots ship to production."

---

## 10. Success Metrics (Hackathon)

- **Submission deadline met:** 5:00 PM May 18 ✓
- **Demo runs cleanly in <90 seconds without errors** in a rehearsal
- **Top 10 selection** for main-stage live pitch May 19
- **Win signal:** judges ask "is this a product? when can I use it?"

---

## 11. Pitch Narrative (For Submission Form & Stage)

### Problem (15s)
Every Fortune 500 has 30+ AI pilots and only a handful in production. The bottleneck is governance. Agents that can spend money, provision infrastructure, or call paid APIs are blocked from production because there's no way to bound what they do.

### Solution (15s)
Ledger is the policy and evidence layer for autonomous agent actions. Drop our middleware in front of any agent's tool calls. Define policy in plain English. Approve edge cases in Slack. Every decision logged with hash-verified evidence.

### Demo (90s)
[See section 9]

### Why now (15s)
Agentic AI is here. EU AI Act enforcement is starting. SOC 2 is asking about AI controls. Enterprises need this *Monday*, not "next year."

### Why us (15s)
We built this in a day with the right stack — Gemini for policy reasoning, FastAPI for performance, hash-chained evidence for trust. Production-ready architecture, hackathon-ready demo.

---

## 12. Open Questions / Risks

- **Slack interactive buttons:** ngrok may be flaky on convention center wifi. Mitigation: dashboard-side approval as fallback.
- **Gemini latency:** if intent-alignment call is slow, agents see lag. Mitigation: deterministic checks first, only call Gemini when deterministic checks pass and an intent check is required.
- **Demo timing:** if too many slow steps, demo runs over. Mitigation: rehearse 3x, pre-stage agent script with timing.
