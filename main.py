"""
Ledger — The Policy & Evidence Layer for Autonomous Agent Spend

A FastAPI proxy that sits between AI agents and the world. Agents POST intended
actions; Ledger evaluates them against policy, escalates edge cases to Slack
for human approval, and logs every decision with a SHA-256 hash for audit.
"""
import os
import json
import hashlib
import sqlite3
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import contextmanager


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader — no external dependency. Skips comments and blanks."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent / ".env")


import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------- Config ----------
DB_PATH = os.environ.get("LEDGER_DB", "ledger.db")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

log = logging.getLogger("ledger")
logging.basicConfig(level=logging.INFO, format="[ledger] %(message)s")

# ---------- Gemini ----------
# We talk to Gemini via the REST API directly (not the SDK). The SDK's gRPC
# transport has been flaky on Python 3.14 — silent 504s and retries that ignore
# request timeouts. Plain REST is simpler, faster, and easy to bound.
_gemini_ready = bool(GEMINI_API_KEY)
_GEMINI_REST_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def gemini_json(prompt: str, schema: Optional[dict] = None, system: Optional[str] = None,
                timeout_s: float = 10.0) -> dict:
    """Call Gemini's REST API and parse a JSON response. Returns {} on failure."""
    if not _gemini_ready:
        return {}
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }
    if schema is not None:
        body["generationConfig"]["responseSchema"] = schema
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    try:
        r = requests.post(
            _GEMINI_REST_URL.format(model=GEMINI_MODEL),
            headers={"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=timeout_s,
        )
        if r.status_code != 200:
            log.warning("Gemini REST %d: %s", r.status_code, r.text[:160])
            return {}
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text) if text else {}
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini call failed: %s", e)
        return {}


# ---------- DB ----------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            task TEXT,
            action_json TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reasoning TEXT,
            rule_violated TEXT,
            sha256 TEXT NOT NULL,
            human_decision TEXT,
            decided_at TEXT,
            decided_by TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS policies (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            rule_json TEXT NOT NULL,
            source TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    cur.execute("SELECT COUNT(*) FROM policies")
    if cur.fetchone()[0] == 0:
        # Note: intent_alignment is available as a rule type but not seeded by default —
        # it would call Gemini on every approved action, which is wasteful for the demo
        # and burns free-tier quota. Add it via Policy Copilot or POST /policies if needed.
        defaults = [
            {"type": "budget_cap", "limit_usd": 5000, "scope": "single_action",
             "description": "No single action over $5,000 without approval"},
            {"type": "category_block", "categories": ["gpu_provisioning"],
             "requires_approval": True, "approval_threshold_usd": 1000,
             "description": "GPU provisioning over $1,000 requires approval"},
        ]
        for rule in defaults:
            cur.execute(
                "INSERT INTO policies (id, created_at, rule_json, source) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), now_iso(), json.dumps(rule), "default"),
            )
    conn.commit()
    conn.close()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------- Models ----------
class ActionRequest(BaseModel):
    agent_id: str
    task: str
    action: dict


class ActionResponse(BaseModel):
    action_id: str
    verdict: str
    reasoning: str
    rule_violated: Optional[str] = None
    sha256: str
    timestamp: str


class ApprovalRequest(BaseModel):
    decision: str  # "approved" | "denied"
    decided_by: str


class PolicyFromTextRequest(BaseModel):
    text: str


# ---------- Policy Engine ----------
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_active_policies() -> list:
    with db() as conn:
        rows = conn.execute("SELECT rule_json FROM policies WHERE active=1").fetchall()
    return [json.loads(r["rule_json"]) for r in rows]


def intent_alignment_check(task: str, action: dict) -> dict:
    """Return {'aligned': bool, 'reasoning': str}. Falls back to {'aligned': True, ...} if Gemini unavailable."""
    if not _gemini_ready:
        return {"aligned": True, "reasoning": "Intent check skipped (Gemini not configured)."}

    system = (
        "You are a strict policy reviewer evaluating whether an AI agent's "
        "proposed action plausibly serves its stated task. Be skeptical. "
        "Provisioning expensive infrastructure for a small UI bug, paying a "
        "stranger when the task is internal — these are misalignments. "
        "Reply with strict JSON: {\"aligned\": bool, \"reasoning\": one short sentence}."
    )
    prompt = (
        f"Agent task: {task}\n"
        f"Proposed action: {json.dumps(action)}\n\n"
        "Does this action plausibly serve the stated task? "
        "Answer aligned=true ONLY if the action is a reasonable step toward the task."
    )
    schema = {
        "type": "object",
        "properties": {
            "aligned": {"type": "boolean"},
            "reasoning": {"type": "string"},
        },
        "required": ["aligned", "reasoning"],
    }
    result = gemini_json(prompt, schema=schema, system=system)
    if not result or "aligned" not in result:
        return {"aligned": True, "reasoning": "Intent check unavailable; deferring to deterministic policy."}
    return {
        "aligned": bool(result["aligned"]),
        "reasoning": str(result.get("reasoning", ""))[:500],
    }


def evaluate_action(task: str, action: dict, policies: list) -> dict:
    """Returns {'verdict', 'reasoning', 'rule_violated'}."""
    raw_cost = action.get("cost_usd", 0) or 0
    try:
        cost = float(raw_cost)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"action.cost_usd must be a number (got {raw_cost!r})",
        )
    category = (action.get("category") or "").lower()
    now = datetime.now(timezone.utc)
    day_name = DAY_NAMES[now.weekday()]
    hour = now.hour

    intent_rule_present = False

    # Deterministic checks first.
    for rule in policies:
        rtype = rule.get("type")

        if rtype == "budget_cap":
            limit = float(rule.get("limit_usd", 0))
            scope = rule.get("scope", "single_action")
            if scope == "single_action" and cost > limit:
                return {
                    "verdict": "pending_approval",
                    "reasoning": f"Action cost ${cost:,.2f} exceeds the ${limit:,.2f} single-action budget cap. Human approval required.",
                    "rule_violated": f"budget_cap (limit ${limit:,.2f})",
                }

        elif rtype == "category_block":
            cats = [c.lower() for c in rule.get("categories", [])]
            if category and (category in cats or "all" in cats):
                threshold = float(rule.get("approval_threshold_usd", 0))
                if cost > threshold:
                    return {
                        "verdict": "pending_approval",
                        "reasoning": f"Category '{category}' over ${threshold:,.2f} requires approval. This action: ${cost:,.2f}.",
                        "rule_violated": f"category_block ({category})",
                    }

        elif rtype == "time_window":
            block_days = rule.get("block_days", [])
            block_hours = rule.get("block_hours", [])
            block_cats = [c.lower() for c in rule.get("block_categories", [])]
            cat_matches = (not block_cats) or "all" in block_cats or category in block_cats
            day_match = day_name in block_days if block_days else False
            hour_match = hour in block_hours if block_hours else False
            if cat_matches and (day_match or hour_match):
                trigger = []
                if day_match:
                    trigger.append(f"day={day_name}")
                if hour_match:
                    trigger.append(f"hour={hour:02d}")
                return {
                    "verdict": "pending_approval",
                    "reasoning": f"Action falls inside a restricted time window ({', '.join(trigger)}). Human approval required.",
                    "rule_violated": f"time_window ({rule.get('description', 'restricted window')})",
                }

        elif rtype == "intent_alignment":
            intent_rule_present = bool(rule.get("use_llm", False))

    # Gemini intent check only runs if deterministic checks pass, a rule asks for it,
    # and the cost is non-trivial (cheap actions skip the LLM call to conserve quota).
    INTENT_COST_FLOOR = 100.0
    if intent_rule_present and _gemini_ready and cost >= INTENT_COST_FLOOR:
        intent = intent_alignment_check(task, action)
        if not intent["aligned"]:
            return {
                "verdict": "pending_approval",
                "reasoning": f"Intent drift flagged by Gemini: {intent['reasoning']}",
                "rule_violated": "intent_alignment",
            }

    return {
        "verdict": "approved",
        "reasoning": "Action within policy. Auto-approved.",
        "rule_violated": None,
    }


# ---------- Slack ----------
def post_slack_approval_request(
    action_id: str,
    agent_id: str,
    task: str,
    action: dict,
    reasoning: str,
    rule_violated: Optional[str],
    sha256: str,
) -> None:
    if not SLACK_WEBHOOK_URL:
        log.info("Slack webhook not configured; skipping Slack ping for %s", action_id)
        return
    cost = float(action.get("cost_usd", 0) or 0)
    desc = action.get("description") or "(no description)"
    category = action.get("category") or "—"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🛑 Action requires approval", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Agent*\n`{agent_id}`"},
                {"type": "mrkdwn", "text": f"*Task*\n{task}"},
                {"type": "mrkdwn", "text": f"*Action*\n{desc}"},
                {"type": "mrkdwn", "text": f"*Cost*\n${cost:,.2f}"},
                {"type": "mrkdwn", "text": f"*Category*\n{category}"},
                {"type": "mrkdwn", "text": f"*Rule violated*\n{rule_violated or '—'}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Why this triggered*\n{reasoning}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"action_id: `{action_id}`  •  sha256: `{sha256[:12]}…`"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "action_id": "deny_action",
                    "value": action_id,
                },
            ],
        },
    ]
    try:
        requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": f"Action requires approval ({rule_violated or 'policy'})", "blocks": blocks},
            timeout=4,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Slack post failed for %s: %s", action_id, e)


# ---------- Policy Copilot ----------
POLICY_COPILOT_SYSTEM = """You convert plain-English governance policies into a single JSON rule.

Return EXACTLY ONE rule. The rule must conform to one of these schemas:

1) budget_cap:
   {"type":"budget_cap","limit_usd": <number>, "scope":"single_action"|"hourly"|"daily","description":"<string>"}

2) category_block:
   {"type":"category_block","categories":[<lowercase strings>],"requires_approval": true,"approval_threshold_usd": <number>,"description":"<string>"}

3) time_window:
   {"type":"time_window","block_days":[<"Mon"|"Tue"|"Wed"|"Thu"|"Fri"|"Sat"|"Sun">],"block_hours":[<0-23 ints>],"block_categories":[<lowercase strings or "all">],"description":"<string>"}

4) intent_alignment:
   {"type":"intent_alignment","use_llm": true,"description":"<string>"}

Rules:
- Use lowercase category names like "gpu_provisioning", "storage", "compute", "networking", "database", "monitoring", "vendor_payment".
- If the policy mentions weekends, set block_days=["Sat","Sun"].
- If the policy mentions evenings/nights without specifics, set block_hours=[22,23,0,1,2,3,4,5].
- If the policy is about specific spend thresholds, prefer budget_cap.
- If it names a category that needs approval, use category_block (approval_threshold_usd defaults to 0 meaning "always").
- Description must be a single human-readable sentence.
- Output STRICT JSON only. No extra keys, no prose.
"""


REQUIRED_FIELDS = {
    "budget_cap": ["limit_usd", "scope", "description"],
    "category_block": ["categories", "requires_approval", "approval_threshold_usd", "description"],
    "time_window": ["description"],  # at least one of block_days/hours/categories
    "intent_alignment": ["use_llm", "description"],
}


def validate_rule(rule: dict) -> Optional[str]:
    if not isinstance(rule, dict):
        return "rule is not an object"
    rtype = rule.get("type")
    if rtype not in REQUIRED_FIELDS:
        return f"unknown rule type: {rtype}"
    for field in REQUIRED_FIELDS[rtype]:
        if field not in rule:
            return f"missing field: {field}"
    if rtype == "time_window":
        has_any = any(rule.get(k) for k in ("block_days", "block_hours", "block_categories"))
        if not has_any:
            return "time_window must specify block_days, block_hours, or block_categories"
    return None


def generate_policy_from_text(text: str) -> dict:
    if not _gemini_ready:
        raise HTTPException(status_code=503, detail="Gemini not configured. Set GEMINI_API_KEY.")
    prompt = f"Plain-English policy:\n\"\"\"\n{text}\n\"\"\"\n\nReturn one JSON rule."
    rule = gemini_json(prompt, system=POLICY_COPILOT_SYSTEM)
    err = validate_rule(rule)
    if err:
        raise HTTPException(status_code=400, detail={"error": err, "model_output": rule})
    return rule


# ---------- App ----------
app = FastAPI(title="Ledger", description="Policy & Evidence Layer for Autonomous Agent Spend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    banner = (
        "\n"
        "  ┌─────────────────────────────────────────────────────────────┐\n"
        "  │  LEDGER  —  Policy & Evidence Layer for Agent Spend         │\n"
        "  │  Dashboard:  http://localhost:8000                          │\n"
        "  │  API docs:   http://localhost:8000/docs                     │\n"
        f"  │  Gemini:     {'configured' if _gemini_ready else 'not configured (set GEMINI_API_KEY)':<46} │\n"
        f"  │  Slack:      {'configured' if SLACK_WEBHOOK_URL else 'not configured (set SLACK_WEBHOOK_URL)':<46} │\n"
        f"  │  DB:         {DB_PATH:<46} │\n"
        "  └─────────────────────────────────────────────────────────────┘\n"
    )
    print(banner)


@app.post("/agent/action", response_model=ActionResponse)
def agent_action(req: ActionRequest):
    action_id = str(uuid.uuid4())
    ts = now_iso()
    policies = get_active_policies()
    result = evaluate_action(req.task, req.action, policies)

    payload_for_hash = {
        "id": action_id,
        "timestamp": ts,
        "agent_id": req.agent_id,
        "task": req.task,
        "action": req.action,
        "verdict": result["verdict"],
    }
    hash_hex = sha256_hex(payload_for_hash)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO actions
              (id, timestamp, agent_id, task, action_json, verdict, reasoning, rule_violated, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, ts, req.agent_id, req.task,
                json.dumps(req.action), result["verdict"],
                result["reasoning"], result.get("rule_violated"), hash_hex,
            ),
        )

    if result["verdict"] == "pending_approval":
        post_slack_approval_request(
            action_id=action_id,
            agent_id=req.agent_id,
            task=req.task,
            action=req.action,
            reasoning=result["reasoning"],
            rule_violated=result.get("rule_violated"),
            sha256=hash_hex,
        )

    return ActionResponse(
        action_id=action_id,
        verdict=result["verdict"],
        reasoning=result["reasoning"],
        rule_violated=result.get("rule_violated"),
        sha256=hash_hex,
        timestamp=ts,
    )


@app.get("/audit")
def audit_log(limit: int = 100):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/audit/{action_id}")
def audit_one(action_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="action not found")
    return dict(row)


@app.post("/approve/{action_id}")
def approve(action_id: str, body: ApprovalRequest):
    if body.decision not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")
    decided_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="action not found")
        if row["verdict"] != "pending_approval":
            raise HTTPException(
                status_code=409,
                detail=f"action is not pending approval (current verdict: {row['verdict']})",
            )
        if row["human_decision"] is not None:
            raise HTTPException(
                status_code=409,
                detail=f"action already {row['human_decision']} by {row['decided_by']} at {row['decided_at']}",
            )
        conn.execute(
            "UPDATE actions SET human_decision=?, decided_at=?, decided_by=? WHERE id=?",
            (body.decision, decided_at, body.decided_by, action_id),
        )
        updated = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    return dict(updated)


@app.get("/policies")
def list_policies():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, rule_json, source, active FROM policies WHERE active=1 ORDER BY created_at DESC"
        ).fetchall()
    return [{**dict(r), "rule": json.loads(r["rule_json"])} for r in rows]


@app.post("/policies")
def add_policy(rule: dict):
    err = validate_rule(rule)
    if err:
        raise HTTPException(status_code=400, detail=err)
    pid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO policies (id, created_at, rule_json, source) VALUES (?, ?, ?, ?)",
            (pid, now_iso(), json.dumps(rule), "manual"),
        )
    return {"id": pid, "rule": rule}


@app.post("/policies/from_text")
def policy_from_text(req: PolicyFromTextRequest):
    rule = generate_policy_from_text(req.text)
    pid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO policies (id, created_at, rule_json, source) VALUES (?, ?, ?, ?)",
            (pid, now_iso(), json.dumps(rule), "copilot"),
        )
    return {"id": pid, "rule": rule, "source_text": req.text}


@app.delete("/policies/{policy_id}")
def deactivate_policy(policy_id: str):
    with db() as conn:
        cur = conn.execute("UPDATE policies SET active=0 WHERE id=?", (policy_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="policy not found")
    return {"id": policy_id, "active": False}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini": _gemini_ready,
        "slack": bool(SLACK_WEBHOOK_URL),
        "db": DB_PATH,
        "time": now_iso(),
    }


# ---------- Dashboard ----------
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Ledger — Policy & Evidence Layer</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root {
    --bg: #0a0a0a;
    --panel: #111111;
    --panel-2: #161616;
    --border: #1f1f1f;
    --text: #e5e5e5;
    --muted: #8a8a8a;
    --green: #10b981;
    --red: #ef4444;
    --yellow: #f59e0b;
    --blue: #3b82f6;
  }
  html, body { background: var(--bg); color: var(--text); }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  .mono { font-family: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", monospace; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; }
  .panel-2 { background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; }
  .row { border-left: 3px solid transparent; transition: background 120ms ease; }
  .row:hover { background: #161616; }
  .row.approved { border-left-color: var(--green); }
  .row.blocked  { border-left-color: var(--red); }
  .row.pending_approval { border-left-color: var(--yellow); }
  .badge { font-size: 10px; font-weight: 600; letter-spacing: 0.04em; padding: 2px 7px; border-radius: 3px; text-transform: uppercase; }
  .badge.approved { background: rgba(16,185,129,0.12); color: var(--green); border: 1px solid rgba(16,185,129,0.25); }
  .badge.blocked  { background: rgba(239,68,68,0.12); color: var(--red); border: 1px solid rgba(239,68,68,0.25); }
  .badge.pending_approval { background: rgba(245,158,11,0.12); color: var(--yellow); border: 1px solid rgba(245,158,11,0.25); }
  .badge.denied { background: rgba(239,68,68,0.12); color: var(--red); border: 1px solid rgba(239,68,68,0.25); }
  .fade-in { animation: fade-in .2s ease-out; }
  @keyframes fade-in { from { opacity: 0; transform: translateY(-2px); } to { opacity: 1; transform: none; } }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 4px; font-size: 12px; font-weight: 500; border: 1px solid var(--border); background: #1a1a1a; color: var(--text); cursor: pointer; }
  .btn:hover { background: #222; }
  .btn-primary { background: rgba(16,185,129,0.15); border-color: rgba(16,185,129,0.4); color: var(--green); }
  .btn-primary:hover { background: rgba(16,185,129,0.22); }
  .btn-danger  { background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.4); color: var(--red); }
  .btn-danger:hover { background: rgba(239,68,68,0.22); }
  textarea { resize: vertical; }
  ::selection { background: rgba(59,130,246,0.4); }
  .scrollbar::-webkit-scrollbar { width: 8px; height: 8px; }
  .scrollbar::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 4px; }
  .scrollbar::-webkit-scrollbar-track { background: transparent; }
  .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
  .dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1.6s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.35 } }
</style>
</head>
<body class="min-h-screen">

<header class="border-b border-[var(--border)] px-6 py-4 flex items-center justify-between">
  <div class="flex items-center gap-3">
    <div class="mono text-lg font-semibold tracking-tight">LEDGER</div>
    <div class="text-[11px] text-[var(--muted)] uppercase tracking-widest">Policy & Evidence Layer for Agent Spend</div>
  </div>
  <div class="flex items-center gap-4 text-xs text-[var(--muted)]">
    <span><span class="dot live"></span> <span id="live-text">live</span></span>
    <span id="health-state" class="mono"></span>
  </div>
</header>

<main class="grid grid-cols-12 gap-4 p-4">
  <!-- Live action stream -->
  <section class="col-span-12 lg:col-span-8 panel">
    <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
      <div class="flex items-center gap-3">
        <h2 class="text-sm font-semibold uppercase tracking-widest">Live Action Stream</h2>
        <span id="action-count" class="text-xs text-[var(--muted)] mono"></span>
      </div>
      <div class="flex items-center gap-3 text-xs text-[var(--muted)]">
        <span><span class="badge approved">approved</span></span>
        <span><span class="badge pending_approval">pending</span></span>
        <span><span class="badge blocked">blocked</span></span>
      </div>
    </div>
    <div id="actions" class="scrollbar overflow-auto max-h-[78vh]"></div>
    <div id="empty" class="px-6 py-12 text-center text-sm text-[var(--muted)] hidden">
      Waiting for agent actions…<br>
      <span class="mono text-xs">run <span class="text-[var(--text)]">python3 demo_agent.py</span> to start the demo</span>
    </div>
  </section>

  <!-- Right column -->
  <section class="col-span-12 lg:col-span-4 flex flex-col gap-4">
    <!-- Policies -->
    <div class="panel flex-1">
      <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <h2 class="text-sm font-semibold uppercase tracking-widest">Active Policies</h2>
        <span id="policy-count" class="text-xs text-[var(--muted)] mono"></span>
      </div>
      <div id="policies" class="scrollbar overflow-auto max-h-[36vh] p-3 flex flex-col gap-2"></div>
    </div>

    <!-- Policy Copilot -->
    <div class="panel">
      <div class="px-4 py-3 border-b border-[var(--border)] flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-widest">Policy Copilot</h2>
        <span class="text-[10px] text-[var(--muted)] uppercase tracking-widest">Gemini</span>
      </div>
      <div class="p-3">
        <textarea id="copilot-input"
          class="w-full bg-[var(--panel-2)] border border-[var(--border)] rounded p-2 text-sm focus:outline-none focus:border-[var(--blue)]"
          rows="3"
          placeholder="Describe a policy in plain English… e.g. 'Agents can't spin up GPU clusters on weekends without VP approval.'"></textarea>
        <div class="flex items-center justify-between mt-2">
          <span id="copilot-status" class="text-xs text-[var(--muted)]">Press Enter to generate · Shift+Enter for newline</span>
          <button id="copilot-submit" class="btn btn-primary">Generate rule</button>
        </div>
        <div id="copilot-result" class="mt-3 hidden"></div>
      </div>
    </div>
  </section>
</main>

<script>
const ENDPOINTS = {
  audit: '/audit?limit=50',
  policies: '/policies',
  fromText: '/policies/from_text',
  approve: (id) => `/approve/${id}`,
  health: '/health',
};

const state = { actions: [], policies: [], expanded: new Set() };

function fmtMoney(n) {
  const num = Number(n || 0);
  return '$' + num.toLocaleString('en-US', { minimumFractionDigits: num % 1 ? 2 : 0, maximumFractionDigits: 2 });
}

function timeShort(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const ageMs = Date.now() - d.getTime();
  if (ageMs < 60_000) return Math.max(0, Math.round(ageMs/1000)) + 's ago';
  return d.toLocaleTimeString([], { hour12: false });
}

function truncHash(h) {
  if (!h) return '';
  return h.slice(0, 6) + '…' + h.slice(-4);
}

function badge(verdict) {
  return `<span class="badge ${verdict}">${verdict.replace('_',' ')}</span>`;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderActions() {
  const root = document.getElementById('actions');
  const empty = document.getElementById('empty');
  if (!state.actions.length) {
    root.innerHTML = '';
    empty.classList.remove('hidden');
    document.getElementById('action-count').textContent = '';
    return;
  }
  empty.classList.add('hidden');
  document.getElementById('action-count').textContent = state.actions.length + ' actions';

  root.innerHTML = state.actions.map(a => {
    let action = {};
    try { action = JSON.parse(a.action_json); } catch (_e) {}
    const cost = action.cost_usd ?? 0;
    const desc = action.description || '(no description)';
    const cat = action.category || '—';
    const isExpanded = state.expanded.has(a.id);
    const decided = a.human_decision;
    return `
      <div class="row ${a.verdict} px-4 py-3 border-b border-[var(--border)] cursor-pointer fade-in" data-id="${a.id}">
        <div class="flex items-center gap-3 text-sm">
          <span class="mono text-[var(--muted)] text-xs w-16">${timeShort(a.timestamp)}</span>
          <span class="mono text-[var(--muted)] text-xs w-40 truncate" title="${escapeHtml(a.agent_id)}">${escapeHtml(a.agent_id)}</span>
          <span class="flex-1 truncate">${escapeHtml(desc)}</span>
          <span class="mono text-xs text-right w-24">${escapeHtml(fmtMoney(cost))}</span>
          ${badge(a.verdict)}
          ${decided ? `<span class="badge ${decided === 'approved' ? 'approved' : 'denied'}">${decided}</span>` : ''}
        </div>
        ${isExpanded ? `
          <div class="mt-3 ml-16 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
            <div><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Task</div><div>${escapeHtml(a.task)}</div></div>
            <div><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Category</div><div class="mono">${escapeHtml(cat)}</div></div>
            <div class="col-span-2"><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Reasoning</div><div>${escapeHtml(a.reasoning || '—')}</div></div>
            ${a.rule_violated ? `<div class="col-span-2"><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Rule violated</div><div class="mono">${escapeHtml(a.rule_violated)}</div></div>` : ''}
            <div class="col-span-2"><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">SHA-256</div>
              <div class="mono cursor-copy" title="click to copy" data-copy="${a.sha256}">${truncHash(a.sha256)}</div></div>
            <div class="col-span-2"><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Action ID</div><div class="mono">${a.id}</div></div>
            <div class="col-span-2"><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Action payload</div>
              <pre class="mono bg-[var(--panel-2)] border border-[var(--border)] rounded p-2 overflow-auto">${escapeHtml(JSON.stringify(action, null, 2))}</pre></div>
            ${decided ? `
              <div><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Decided by</div><div class="mono">${escapeHtml(a.decided_by || '—')}</div></div>
              <div><div class="text-[var(--muted)] uppercase tracking-widest text-[10px] mb-1">Decided at</div><div class="mono">${escapeHtml(a.decided_at || '—')}</div></div>
            ` : ''}
            ${a.verdict === 'pending_approval' && !decided ? `
              <div class="col-span-2 flex gap-2 mt-1">
                <button class="btn btn-primary" data-approve="${a.id}">Approve</button>
                <button class="btn btn-danger"  data-deny="${a.id}">Deny</button>
              </div>` : ''}
          </div>` : ''}
      </div>`;
  }).join('');
}

function renderPolicies() {
  const root = document.getElementById('policies');
  document.getElementById('policy-count').textContent = state.policies.length + ' active';
  if (!state.policies.length) {
    root.innerHTML = '<div class="text-xs text-[var(--muted)] px-2 py-4">No policies active.</div>';
    return;
  }
  root.innerHTML = state.policies.map(p => {
    const r = p.rule;
    const src = p.source || 'manual';
    const srcColor = src === 'default' ? 'text-[var(--muted)]' : src === 'copilot' ? 'text-[var(--blue)]' : 'text-[var(--green)]';
    return `
      <div class="panel-2 px-3 py-2">
        <div class="flex items-start justify-between gap-2">
          <div>
            <div class="text-sm">${escapeHtml(r.description || r.type)}</div>
            <div class="mono text-[11px] text-[var(--muted)] mt-1">${escapeHtml(r.type)}</div>
          </div>
          <span class="text-[10px] uppercase tracking-widest ${srcColor}">${escapeHtml(src)}</span>
        </div>
      </div>`;
  }).join('');
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function poll() {
  try {
    const [acts, pols] = await Promise.all([
      fetchJSON(ENDPOINTS.audit),
      fetchJSON(ENDPOINTS.policies),
    ]);
    state.actions = acts;
    state.policies = pols;
    renderActions();
    renderPolicies();
    document.getElementById('live-text').textContent = 'live';
  } catch (e) {
    document.getElementById('live-text').textContent = 'reconnecting…';
  }
}

async function refreshHealth() {
  try {
    const h = await fetchJSON(ENDPOINTS.health);
    const tags = [];
    tags.push(h.gemini ? 'gemini✓' : 'gemini✗');
    tags.push(h.slack ? 'slack✓' : 'slack✗');
    document.getElementById('health-state').textContent = tags.join(' · ');
  } catch (_) { /* ignore */ }
}

document.addEventListener('click', async (ev) => {
  const row = ev.target.closest('.row');
  const approveBtn = ev.target.closest('[data-approve]');
  const denyBtn = ev.target.closest('[data-deny]');
  const copyEl = ev.target.closest('[data-copy]');

  if (copyEl) {
    ev.stopPropagation();
    try { await navigator.clipboard.writeText(copyEl.dataset.copy); copyEl.textContent = 'copied ✓'; setTimeout(() => poll(), 800); } catch(_) {}
    return;
  }

  if (approveBtn || denyBtn) {
    ev.stopPropagation();
    const id = (approveBtn || denyBtn).dataset.approve || (approveBtn || denyBtn).dataset.deny;
    const decision = approveBtn ? 'approved' : 'denied';
    try {
      await fetchJSON(ENDPOINTS.approve(id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, decided_by: 'dashboard-user' }),
      });
      await poll();
    } catch (e) { alert('Failed: ' + e.message); }
    return;
  }

  if (row) {
    const id = row.dataset.id;
    if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
    renderActions();
  }
});

// Policy Copilot
const ci = document.getElementById('copilot-input');
const submitBtn = document.getElementById('copilot-submit');
const statusEl = document.getElementById('copilot-status');
const resultEl = document.getElementById('copilot-result');

async function submitCopilot() {
  const text = ci.value.trim();
  if (!text) return;
  statusEl.textContent = 'Generating with Gemini…';
  submitBtn.disabled = true; submitBtn.classList.add('opacity-50');
  resultEl.classList.add('hidden');
  try {
    const out = await fetchJSON(ENDPOINTS.fromText, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    statusEl.textContent = 'Rule added to active policies.';
    resultEl.classList.remove('hidden');
    resultEl.innerHTML = `
      <div class="panel-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-widest text-[var(--blue)] mb-1">Generated rule</div>
        <pre class="mono text-xs overflow-auto">${escapeHtml(JSON.stringify(out.rule, null, 2))}</pre>
      </div>`;
    ci.value = '';
    await poll();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
  } finally {
    submitBtn.disabled = false; submitBtn.classList.remove('opacity-50');
  }
}

submitBtn.addEventListener('click', submitCopilot);
ci.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitCopilot(); }
});

poll();
refreshHealth();
setInterval(poll, 1000);
setInterval(refreshHealth, 5000);
</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
