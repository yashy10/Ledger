# Ledger — Demo Script

**Goal:** 90-second live demo that makes a Fortune 500 CIO go "I need this Monday."

**Audience:** Mixed enterprise C-suite (CIOs, Chief AI Officers, CFOs from State Street, Ford Credit, US Bank, etc.) + technical judges. Half are from regulated industries. None will be reading your code.

**Stage setup:** Laptop with Ledger running locally. Slack open on phone or second screen. Dashboard on main screen.

---

## The 90-Second Script

### Beat 1 — The Hook (0:00–0:12)

> "Show of hands — how many of you have AI agents in production right now?
> 
> [pause]
> 
> Not many. And the reason is *not* the model. The reason is this: every CIO and CFO in this room is asking the same question. *If we give an agent access to our infrastructure, our APIs, our money — what stops it from spending fifty thousand dollars while we sleep?*
> 
> Right now, the answer is *nothing.* That's why your pilots are stuck."

### Beat 2 — Frame the Solution (0:12–0:25)

> "We built Ledger. It's the policy and evidence layer for autonomous agent actions. Every action your agent wants to take flows through us first. We enforce policy in real time. We escalate edge cases to humans. We log everything with cryptographic hashes for your auditors.
> 
> Let me show you."

### Beat 3 — Watch It Work (0:25–0:55)

*Switch to dashboard. Click "Run Demo Agent."*

> "This is a typical DevOps agent provisioning infrastructure for a customer demo. Watch the dashboard."

*Actions stream in:*
- Storage — $12 — ✅ APPROVED
- Database — $0.50 — ✅ APPROVED  
- VM — $42 — ✅ APPROVED
- Load balancer — $8 — ✅ APPROVED
- **100 H100 GPUs — $48,000 — 🛑 POLICY VIOLATION**

*Slack ping plays.*

> "Forty-eight thousand dollars in one action. Could be a prompt injection. Could be a logic loop. Doesn't matter — Ledger caught it. Look at Slack."

### Beat 4 — Human in the Loop (0:55–1:15)

*Show Slack card.*

> "Full context. Which agent. What task. What it tried. Why we caught it. The approver — that's me — clicks Deny."

*Click Deny.*

> "Action blocked. Agent informed. And here's the part your compliance team cares about — the audit log."

*Click the blocked entry in the dashboard.*

> "Every action, hashed with SHA-256. Timestamp. Reasoning. Who decided what. This is your SOC 2 evidence. This is your EU AI Act artifact. This is what unsticks your AI pilots."

### Beat 5 — The Mic Drop (1:15–1:30)

*Click into Policy Copilot.*

> "Last thing. Your CFO doesn't write Python. Your risk officer doesn't write Python. So we built this."

*Type into Policy Copilot:* `"Agents cannot spin up GPU clusters on weekends without VP approval, ever."`

*Hit enter. New rule appears in the policy list.*

> "Plain English. Live policy. No engineering bottleneck.
> 
> Your CFO writes the rules. Your agents follow them. Your auditor has the evidence. Your AI pilots ship to production.
> 
> That's Ledger. Thank you."

---

## Critical Delivery Notes

### Tone
- **Calm, not hyped.** This audience is suspicious of demo theatrics. Sound like a serious infrastructure founder, not a YC pitch.
- **Use enterprise vocabulary:** "governance," "policy," "evidence," "audit," "compliance," "SOC 2," "EU AI Act," "human-in-the-loop"
- **Avoid:** "AI safety," "guardrails" (the word, not the concept), "alignment" (overloaded), "magic," "amazing"

### Pacing
- Slow down on the dollar amount — "**Forty. Eight. Thousand. Dollars.**" — let the room react
- Pause after Slack ping to let people register
- Slow on "SOC 2... EU AI Act... NIST AI RMF" — these are the magic words for this room

### What to do if something breaks
- **Slack doesn't fire:** Continue, click into the action in dashboard. Say "in production this goes to your Slack — for the demo we'll handle it here."
- **Gemini is slow:** Don't wait. Move on. "Intent-check happens async in production."
- **Agent script hangs:** Re-run. Joke once: "even the demo agent needs governance." Move on.
- **Dashboard doesn't update:** Refresh. Apologize once. Continue.

### What to say if asked
- *"Is this a product?"* → "We built the prototype today. Architecture is production-grade. We're talking to design partners after this event."
- *"Who's the buyer?"* → "Chief AI Officer or Head of AI Governance, with the Chief Risk Officer as a strong influencer."
- *"How does it compare to [LangChain Guardrails / Lakera / Promptfoo / etc.]?"* → "Those evaluate the model's outputs. Ledger governs the agent's *actions* — the spend, the API calls, the infrastructure changes. Different layer. Complementary."
- *"What about [thing not built]?"* → "That's on our roadmap. For the hackathon we focused on the spending guardrail because it's the most visceral pain point in the room."

---

## Pre-Demo Checklist (run 30 min before pitch)

- [ ] `python3 main.py` is running and reachable on `localhost:8000`
- [ ] `GEMINI_API_KEY` is set
- [ ] `SLACK_WEBHOOK_URL` is set, test message sent successfully
- [ ] DB has been cleared (`rm ledger.db`, restart server, default policies seeded)
- [ ] Dashboard loads in browser, no console errors
- [ ] Demo agent script runs end-to-end in <90s with all expected behaviors
- [ ] Slack ping fires within 2s of trigger action
- [ ] Policy Copilot test sentence generates a valid rule
- [ ] Laptop charger plugged in
- [ ] Wifi tested (or hotspot ready as backup)
- [ ] Screen brightness maxed
- [ ] Browser zoom set so dashboard is readable from 10 feet
