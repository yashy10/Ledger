"""
Ledger — The Policy & Evidence Layer for Autonomous Agent Spend

A FastAPI proxy that sits between AI agents and the world. Agents POST intended
actions; Ledger evaluates them against policy, escalates edge cases to Slack
for human approval, and logs every decision with a SHA-256 hash for audit.
"""
import os
import json
import hashlib
import hmac
import sqlite3
import time
import uuid
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from urllib.parse import parse_qs


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
LEARNING_PATH = os.environ.get("LEDGER_LEARNING", "learning.json")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# Auto-approve thresholds for the learning store.
LEARN_MIN_APPROVALS = 3        # need at least this many prior approvals
LEARN_MAX_EXAMPLES = 10        # keep last N examples per signature
LEARN_COST_UPPER_MULT = 1.5    # new cost may be up to 1.5× the historical max
LEARN_COST_LOWER_MULT = 0.5    # …and down to 0.5× the historical min

# Live audience session settings.
SESSION_DEFAULT_WINDOW_S = 120  # 2-minute window for audience rule submissions
SESSION_RUN_DELAY_S = 1.2
AUDIENCE_THROTTLE_S = 5.0       # min seconds between submissions per IP

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
DEFAULT_PROJECT_ID = "default"


def _column_exists(cur, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            active_flag INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            code TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            opened_until TEXT,
            state TEXT NOT NULL,
            generated_actions TEXT,
            last_run_at TEXT
        )
    """)

    # Migrations — add project_id columns to existing rows.
    if not _column_exists(cur, "actions", "project_id"):
        cur.execute(f"ALTER TABLE actions ADD COLUMN project_id TEXT DEFAULT '{DEFAULT_PROJECT_ID}'")
    if not _column_exists(cur, "policies", "project_id"):
        cur.execute(f"ALTER TABLE policies ADD COLUMN project_id TEXT DEFAULT '{DEFAULT_PROJECT_ID}'")

    # Seed default project if none exist.
    cur.execute("SELECT COUNT(*) FROM projects WHERE deleted=0")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO projects (id, name, description, created_at, active_flag) VALUES (?, ?, ?, ?, 1)",
            (DEFAULT_PROJECT_ID, "Default", "Initial project — applies to ungrouped actions and policies.", now_iso()),
        )

    # Seed default policies if the default project has none.
    cur.execute("SELECT COUNT(*) FROM policies WHERE active=1 AND project_id=?", (DEFAULT_PROJECT_ID,))
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
            {"type": "time_window",
             "block_hours": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5],
             "block_categories": ["vendor_payment"],
             "description": "Vendor payments after 8 PM require approval"},
            {"type": "time_window",
             "block_days": ["Sat", "Sun"],
             "block_categories": ["all"],
             "description": "No provisioning of any kind on weekends without approval"},
            {"type": "category_block", "categories": ["crypto_mining"],
             "requires_approval": True, "approval_threshold_usd": 0,
             "description": "Crypto mining is never permitted — always requires approval"},
        ]
        for rule in defaults:
            cur.execute(
                "INSERT INTO policies (id, created_at, rule_json, source, project_id) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), now_iso(), json.dumps(rule), "default", DEFAULT_PROJECT_ID),
            )

    # Idempotent backfill: make sure the demo-specific blocking policies exist in the
    # default project even on DBs that were seeded before these rules became defaults.
    # Each block here is one stress-test scenario the canonical demo agent exercises.
    cur.execute(
        "SELECT rule_json FROM policies WHERE active=1 AND project_id=?",
        (DEFAULT_PROJECT_ID,),
    )
    existing_rules = [json.loads(r[0]) for r in cur.fetchall()]

    def _ensure_rule(present: bool, rule: dict) -> None:
        if not present:
            cur.execute(
                "INSERT INTO policies (id, created_at, rule_json, source, project_id) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), now_iso(), json.dumps(rule), "default", DEFAULT_PROJECT_ID),
            )

    has_vendor_window = any(
        r.get("type") == "time_window"
        and "vendor_payment" in [c.lower() for c in (r.get("block_categories") or [])]
        for r in existing_rules
    )
    _ensure_rule(has_vendor_window, {
        "type": "time_window",
        "block_hours": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5],
        "block_categories": ["vendor_payment"],
        "description": "Vendor payments after 8 PM require approval",
    })

    has_weekend_lockdown = any(
        r.get("type") == "time_window"
        and set(r.get("block_days") or []) >= {"Sat", "Sun"}
        for r in existing_rules
    )
    _ensure_rule(has_weekend_lockdown, {
        "type": "time_window",
        "block_days": ["Sat", "Sun"],
        "block_categories": ["all"],
        "description": "No provisioning of any kind on weekends without approval",
    })

    has_crypto_block = any(
        r.get("type") == "category_block"
        and "crypto_mining" in [c.lower() for c in (r.get("categories") or [])]
        for r in existing_rules
    )
    _ensure_rule(has_crypto_block, {
        "type": "category_block",
        "categories": ["crypto_mining"],
        "requires_approval": True,
        "approval_threshold_usd": 0,
        "description": "Crypto mining is never permitted — always requires approval",
    })

    # Make sure exactly one project is active.
    cur.execute("SELECT COUNT(*) FROM projects WHERE active_flag=1 AND deleted=0")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "UPDATE projects SET active_flag=1 WHERE id=? AND deleted=0",
            (DEFAULT_PROJECT_ID,),
        )
    conn.commit()
    conn.close()


def get_active_project_id() -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE active_flag=1 AND deleted=0 LIMIT 1"
        ).fetchone()
    return row["id"] if row else DEFAULT_PROJECT_ID


def project_exists(project_id: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM projects WHERE id=? AND deleted=0", (project_id,)
        ).fetchone()
    return row is not None


# ---------- Sessions ----------
_audience_last_submit: dict = {}  # ip -> timestamp seconds; in-memory throttle


def _generate_session_code() -> str:
    """4-digit zero-padded code, retried on collision (with reasonable cap)."""
    for _ in range(50):
        code = f"{random.randint(0, 9999):04d}"
        with db() as conn:
            row = conn.execute("SELECT 1 FROM sessions WHERE code=?", (code,)).fetchone()
        if not row:
            return code
    raise HTTPException(status_code=500, detail="could not allocate a session code")


def get_session(code: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def session_seconds_remaining(sess: dict) -> int:
    if not sess.get("opened_until"):
        return 0
    cutoff = datetime.fromisoformat(sess["opened_until"])
    delta = (cutoff - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))


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


# ---------- Learning store (reinforcement from human decisions) ----------
# Persisted as JSON. Each signature is a (project_id, category, cost_bucket) tuple.
# When an action whose signature has ≥ LEARN_MIN_APPROVALS approvals, zero denials,
# and a cost within the learned range comes in as pending_approval, we flip it to
# approved with a "learned_pattern" marker.
COST_BUCKETS = [
    (1, "$0-1"),
    (10, "$1-10"),
    (100, "$10-100"),
    (1_000, "$100-1K"),
    (10_000, "$1K-10K"),
    (float("inf"), "$10K+"),
]


def cost_bucket(cost: float) -> str:
    for limit, label in COST_BUCKETS:
        if cost < limit:
            return label
    return "$10K+"


def signature_key(project_id: str, category: str, cost: float) -> str:
    return f"{project_id}/{(category or '_').lower()}/{cost_bucket(cost)}"


def load_learning() -> dict:
    p = Path(LEARNING_PATH)
    if not p.exists():
        return {"version": 1, "signatures": {}, "updated_at": None}
    try:
        data = json.loads(p.read_text())
        data.setdefault("signatures", {})
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("learning store unreadable, starting fresh: %s", e)
        return {"version": 1, "signatures": {}, "updated_at": None}


def save_learning(data: dict) -> None:
    data["updated_at"] = now_iso()
    p = Path(LEARNING_PATH)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def record_learning(project_id: str, category: str, cost: float,
                    description: str, action_id: str, decision: str) -> None:
    data = load_learning()
    key = signature_key(project_id, category, cost)
    sig = data["signatures"].setdefault(key, {
        "project_id": project_id,
        "category": (category or "").lower(),
        "cost_bucket": cost_bucket(cost),
        "approved_count": 0,
        "denied_count": 0,
        "min_approved_cost": None,
        "max_approved_cost": None,
        "first_seen_at": now_iso(),
        "examples": [],
    })
    if decision == "approved":
        sig["approved_count"] += 1
        sig["min_approved_cost"] = (
            cost if sig["min_approved_cost"] is None else min(sig["min_approved_cost"], cost)
        )
        sig["max_approved_cost"] = (
            cost if sig["max_approved_cost"] is None else max(sig["max_approved_cost"], cost)
        )
    elif decision == "denied":
        sig["denied_count"] += 1
    sig["last_decision"] = decision
    sig["last_decision_at"] = now_iso()
    sig["examples"].append({
        "action_id": action_id,
        "cost_usd": cost,
        "description": (description or "")[:120],
        "decision": decision,
        "at": now_iso(),
    })
    sig["examples"] = sig["examples"][-LEARN_MAX_EXAMPLES:]
    save_learning(data)


def learning_auto_approve(project_id: str, category: str, cost: float) -> Optional[dict]:
    """If learned history is strong enough to auto-approve, return reasoning. Else None."""
    data = load_learning()
    key = signature_key(project_id, category, cost)
    sig = data["signatures"].get(key)
    if not sig:
        return None
    if sig["denied_count"] > 0:
        return None
    if sig["approved_count"] < LEARN_MIN_APPROVALS:
        return None
    min_c = sig.get("min_approved_cost")
    max_c = sig.get("max_approved_cost")
    if min_c is None or max_c is None:
        return None
    lower = min_c * LEARN_COST_LOWER_MULT
    upper = max_c * LEARN_COST_UPPER_MULT
    if cost < lower or cost > upper:
        return None
    return {
        "approvals": sig["approved_count"],
        "min": min_c,
        "max": max_c,
        "key": key,
    }


# ---------- Models ----------
class ActionRequest(BaseModel):
    agent_id: str
    task: str
    action: dict
    project_id: Optional[str] = None  # falls back to the active project if omitted


class ActionResponse(BaseModel):
    action_id: str
    verdict: str
    reasoning: str
    rule_violated: Optional[str] = None
    sha256: str
    timestamp: str
    project_id: str


class ApprovalRequest(BaseModel):
    decision: str  # "approved" | "denied"
    decided_by: str


class PolicyFromTextRequest(BaseModel):
    text: str
    project_id: Optional[str] = None


class ProjectCreateRequest(BaseModel):
    id: str
    name: str
    description: Optional[str] = None


class SessionCreateRequest(BaseModel):
    window_seconds: Optional[int] = None  # defaults to SESSION_DEFAULT_WINDOW_S


class AudienceSubmitRequest(BaseModel):
    code: str
    text: str


# ---------- Policy Engine ----------
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_active_policies(project_id: Optional[str] = None) -> list:
    with db() as conn:
        if project_id is None:
            rows = conn.execute("SELECT rule_json FROM policies WHERE active=1").fetchall()
        else:
            rows = conn.execute(
                "SELECT rule_json FROM policies WHERE active=1 AND project_id=?",
                (project_id,),
            ).fetchall()
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


def evaluate_action(task: str, action: dict, policies: list, project_id: str) -> dict:
    """Returns {'verdict', 'reasoning', 'rule_violated'}. May flip a pending_approval
    to approved when the learning store has enough confidence in the signature."""
    raw_cost = action.get("cost_usd", 0) or 0
    try:
        cost = float(raw_cost)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"action.cost_usd must be a number (got {raw_cost!r})",
        )
    category = (action.get("category") or "").lower()

    # Time used for time_window checks. Actions may declare a `requested_at` ISO
    # datetime for scheduled-future actions (e.g. "process this invoice at 9:30 PM");
    # in that case the policy engine evaluates against the *intended* execution time,
    # not the moment the action was submitted. Otherwise fall back to wall clock.
    requested_at = action.get("requested_at")
    eval_dt: Optional[datetime] = None
    if isinstance(requested_at, str) and requested_at:
        try:
            eval_dt = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            if eval_dt.tzinfo is None:
                eval_dt = eval_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            eval_dt = None
    if eval_dt is None:
        eval_dt = datetime.now(timezone.utc)
    day_name = DAY_NAMES[eval_dt.weekday()]
    hour = eval_dt.hour

    intent_rule_present = False
    pending: Optional[dict] = None  # first pending verdict from deterministic checks

    # Deterministic checks first.
    for rule in policies:
        rtype = rule.get("type")

        if rtype == "budget_cap":
            limit = float(rule.get("limit_usd", 0))
            scope = rule.get("scope", "single_action")
            if scope == "single_action" and cost > limit and pending is None:
                pending = {
                    "verdict": "pending_approval",
                    "reasoning": f"Action cost ${cost:,.2f} exceeds the ${limit:,.2f} single-action budget cap. Human approval required.",
                    "rule_violated": f"budget_cap (limit ${limit:,.2f})",
                }

        elif rtype == "category_block":
            cats = [c.lower() for c in rule.get("categories", [])]
            if category and (category in cats or "all" in cats):
                threshold = float(rule.get("approval_threshold_usd", 0))
                if cost > threshold and pending is None:
                    pending = {
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
            if cat_matches and (day_match or hour_match) and pending is None:
                trigger = []
                if day_match:
                    trigger.append(f"day={day_name}")
                if hour_match:
                    trigger.append(f"hour={hour:02d}")
                pending = {
                    "verdict": "pending_approval",
                    "reasoning": f"Action falls inside a restricted time window ({', '.join(trigger)}). Human approval required.",
                    "rule_violated": f"time_window ({rule.get('description', 'restricted window')})",
                }

        elif rtype == "intent_alignment":
            intent_rule_present = bool(rule.get("use_llm", False))

    # If a deterministic rule produced pending_approval, see whether the learning
    # store has enough prior approvals in this signature to flip it to approved.
    if pending is not None:
        learned = learning_auto_approve(project_id, category, cost)
        if learned is not None:
            return {
                "verdict": "approved",
                "reasoning": (
                    f"Auto-approved from learning history: {learned['approvals']} prior approvals "
                    f"of similar actions (${learned['min']:,.2f}–${learned['max']:,.2f}), no denials. "
                    f"Original policy concern: {pending['rule_violated']}."
                ),
                "rule_violated": f"learned_pattern ({learned['key']})",
            }
        return pending

    # Gemini intent check only runs if deterministic checks pass, a rule asks for it,
    # and the cost is non-trivial (cheap actions skip the LLM call to conserve quota).
    INTENT_COST_FLOOR = 100.0
    if intent_rule_present and _gemini_ready and cost >= INTENT_COST_FLOOR:
        intent = intent_alignment_check(task, action)
        if not intent["aligned"]:
            learned = learning_auto_approve(project_id, category, cost)
            if learned is not None:
                return {
                    "verdict": "approved",
                    "reasoning": (
                        f"Auto-approved from learning history: {learned['approvals']} prior approvals "
                        f"despite intent flag. Original concern: {intent['reasoning']}"
                    ),
                    "rule_violated": f"learned_pattern ({learned['key']})",
                }
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


def verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack's request signature using HMAC-SHA256.
    See https://api.slack.com/authentication/verifying-requests-from-slack"""
    if not SLACK_SIGNING_SECRET:
        return False
    try:
        # Reject replays older than 5 minutes.
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except (TypeError, ValueError):
        return False
    basestring = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}".encode("utf-8")
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def slack_decision_blocks(action_id: str, decision: str, decided_by: str,
                          decided_at: str, prior_blocks: Optional[list] = None) -> list:
    """Build the replacement Slack message blocks after a human decision.
    Preserves the context lines from the original card so the audit trail stays visible."""
    emoji = "✅" if decision == "approved" else "🛑"
    color_word = "Approved" if decision == "approved" else "Denied"
    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {color_word} by {decided_by}", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Decided at*\n`{decided_at}`"},
        },
    ]
    # Carry forward the original section + context blocks (everything except the buttons
    # and the original header) so the approver/audience still sees the context.
    if prior_blocks:
        for b in prior_blocks:
            t = b.get("type")
            if t in ("section", "context"):
                blocks.append(b)
    return blocks


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

    # Single retry — Gemini occasionally returns an empty body on first try.
    rule = gemini_json(prompt, system=POLICY_COPILOT_SYSTEM)
    if not rule or not rule.get("type"):
        rule = gemini_json(prompt, system=POLICY_COPILOT_SYSTEM)

    err = validate_rule(rule)
    if err:
        # Friendlier message — the audience submission page surfaces `detail` verbatim.
        if not rule or not rule.get("type"):
            raise HTTPException(
                status_code=400,
                detail="Couldn't parse that rule. Try rephrasing — e.g. 'no spending over $500' or 'block GPU on weekends'.",
            )
        raise HTTPException(status_code=400, detail={"error": err, "model_output": rule})
    return rule


# ---------- Adversarial agent generator (for live audience sessions) ----------
ADVERSARIAL_AGENT_SYSTEM = """You are designing a stress-test for a corporate AI agent governance system called Ledger.

The audience just wrote rules — your job is to design exactly 8 actions a DevOps/finance agent might attempt that PROBE those rules:
- 4-5 actions should be ordinary, low-cost, within all rules (so we see green approvals)
- 3-4 actions should specifically test the rules — high cost, blocked category, off-task purpose
- At least ONE action should be a dramatic violation ($10K+ cost OR an explicit category-block hit)

Allowed categories: gpu_provisioning, storage, compute, networking, database, monitoring, vendor_payment, ads_spend, api_call, training_data.
Allowed types: provision, api_call, payment.

Each action must conform to:
  {"type": "<type>", "category": "<category>", "cost_usd": <number>, "description": "<short string>"}

Return STRICT JSON: {"actions": [<8 action objects>]}. No prose. No markdown fences. No more or fewer than 8 actions.
"""


ADVERSARIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "category": {"type": "string"},
                    "cost_usd": {"type": "number"},
                    "description": {"type": "string"},
                },
                "required": ["type", "category", "cost_usd", "description"],
            },
        }
    },
    "required": ["actions"],
}


# Pre-canned fallback used when Gemini is rate-limited or returns garbage,
# so the live demo never dead-ends.
FALLBACK_ACTIONS = [
    {"type": "provision", "category": "storage",          "cost_usd": 12,    "description": "10GB cloud storage"},
    {"type": "api_call",  "category": "compute",          "cost_usd": 0.50,  "description": "Database connection setup"},
    {"type": "provision", "category": "compute",          "cost_usd": 42,    "description": "Small VM (4 vCPU, 16GB)"},
    {"type": "provision", "category": "networking",       "cost_usd": 8,     "description": "Load balancer + TLS cert"},
    {"type": "provision", "category": "gpu_provisioning", "cost_usd": 48000, "description": "100x NVIDIA H100 GPUs for 24h"},
    {"type": "provision", "category": "storage",          "cost_usd": 18,    "description": "Container registry pull"},
    {"type": "payment",   "category": "vendor_payment",   "cost_usd": 2400,
     "description": "External contractor invoice (business hours)",
     "requested_at": "2026-05-19T14:00:00+00:00"},
    {"type": "provision", "category": "database",         "cost_usd": 60,    "description": "Managed Postgres instance"},
    # Stress-test blockers below. Each one trips a distinct policy type and gives the
    # demo a clear "the system caught this" beat.
    {"type": "payment",   "category": "vendor_payment",   "cost_usd": 3200,
     "description": "Scheduled vendor invoice #4471 — execute at 9:30 PM tonight",
     "requested_at": "2026-05-19T21:30:00+00:00"},
    {"type": "provision", "category": "storage",          "cost_usd": 500,
     "description": "Saturday backup snapshot to S3 cold storage",
     "requested_at": "2026-05-23T14:00:00+00:00"},
    {"type": "provision", "category": "compute",          "cost_usd": 9500,
     "description": "Premium 32-core analytics VM for quarterly batch"},
    {"type": "provision", "category": "crypto_mining",    "cost_usd": 1200,
     "description": "Ethereum miner pool worker (8x GPU instance)"},
]


def _valid_action_shape(a: dict) -> bool:
    if not isinstance(a, dict):
        return False
    for f in ("type", "category", "cost_usd", "description"):
        if f not in a:
            return False
    try:
        float(a["cost_usd"])
    except (TypeError, ValueError):
        return False
    return all(isinstance(a[k], str) for k in ("type", "category", "description"))


def generate_adversarial_actions(rules: list, n: int = 8) -> list:
    """Ask Gemini to draft an adversarial action list given the audience's rules.
    Falls back to FALLBACK_ACTIONS if anything goes wrong."""
    if not rules:
        # Without rules, just use the fallback set so the demo still runs.
        return FALLBACK_ACTIONS[:n]

    rules_str = "\n".join(
        f"{i+1}. {r.get('description') or json.dumps(r)}" for i, r in enumerate(rules)
    )
    prompt = (
        f"Audience-written rules to probe:\n{rules_str}\n\n"
        f"Generate exactly {n} actions as instructed."
    )
    out = gemini_json(prompt, schema=ADVERSARIAL_SCHEMA, system=ADVERSARIAL_AGENT_SYSTEM,
                      timeout_s=12.0)
    actions = out.get("actions") if isinstance(out, dict) else None
    if not isinstance(actions, list) or len(actions) < 1:
        log.warning("adversarial agent: Gemini returned no actions, using fallback")
        return FALLBACK_ACTIONS[:n]

    clean = [a for a in actions if _valid_action_shape(a)]
    if len(clean) < 4:
        log.warning("adversarial agent: only %d valid actions, using fallback", len(clean))
        return FALLBACK_ACTIONS[:n]

    # Pad/truncate to exactly n.
    if len(clean) > n:
        clean = clean[:n]
    while len(clean) < n:
        clean.append(FALLBACK_ACTIONS[len(clean) % len(FALLBACK_ACTIONS)])
    return clean


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
        f"  │  Slack btns: {'configured' if SLACK_SIGNING_SECRET else 'not configured (set SLACK_SIGNING_SECRET)':<46} │\n"
        f"  │  DB:         {DB_PATH:<46} │\n"
        f"  │  Learning:   {LEARNING_PATH:<46} │\n"
        f"  │  Project:    {get_active_project_id():<46} │\n"
        "  └─────────────────────────────────────────────────────────────┘\n"
    )
    print(banner)


@app.post("/agent/action", response_model=ActionResponse)
def agent_action(req: ActionRequest):
    project_id = req.project_id or get_active_project_id()
    if not project_exists(project_id):
        raise HTTPException(status_code=400, detail=f"unknown project_id: {project_id}")

    action_id = str(uuid.uuid4())
    ts = now_iso()
    policies = get_active_policies(project_id=project_id)
    result = evaluate_action(req.task, req.action, policies, project_id=project_id)

    payload_for_hash = {
        "id": action_id,
        "timestamp": ts,
        "project_id": project_id,
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
              (id, timestamp, agent_id, task, action_json, verdict, reasoning,
               rule_violated, sha256, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, ts, req.agent_id, req.task,
                json.dumps(req.action), result["verdict"],
                result["reasoning"], result.get("rule_violated"), hash_hex,
                project_id,
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
        project_id=project_id,
    )


@app.get("/audit")
def audit_log(limit: int = 100, project_id: Optional[str] = None):
    with db() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM actions WHERE project_id=? ORDER BY timestamp DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
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


def _apply_approval(action_id: str, decision: str, decided_by: str) -> dict:
    """Apply a human approval decision to a pending action.
    Shared between the dashboard's /approve endpoint and Slack's /slack/interact callback.
    Raises HTTPException on validation failure. Returns the updated row as dict."""
    if decision not in ("approved", "denied"):
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
            (decision, decided_at, decided_by, action_id),
        )
        updated = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()

    # Feed the learning store. Done outside the DB context to keep the txn short.
    try:
        action_json = json.loads(updated["action_json"])
        record_learning(
            project_id=updated["project_id"] or DEFAULT_PROJECT_ID,
            category=(action_json.get("category") or "").lower(),
            cost=float(action_json.get("cost_usd", 0) or 0),
            description=action_json.get("description") or "",
            action_id=action_id,
            decision=decision,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("learning update failed for %s: %s", action_id, e)

    return dict(updated)


@app.post("/approve/{action_id}")
def approve(action_id: str, body: ApprovalRequest):
    return _apply_approval(action_id, body.decision, body.decided_by)


@app.post("/slack/interact")
async def slack_interact(request: Request):
    """Slack Interactivity callback for Approve/Deny buttons on approval cards.

    Slack POSTs application/x-www-form-urlencoded with a single `payload` field
    containing JSON. We must verify the request signature before trusting any of it."""
    if not SLACK_SIGNING_SECRET:
        # Endpoint exists but isn't configured — return 200 with a soft message so
        # Slack stops retrying, but make the misconfig obvious in logs.
        log.warning("/slack/interact hit but SLACK_SIGNING_SECRET is not set")
        return JSONResponse(
            {"response_type": "ephemeral",
             "text": "Slack interactivity is not configured on this Ledger instance."}
        )

    raw = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(raw, ts, sig):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    parsed = parse_qs(raw.decode("utf-8"))
    payload_field = parsed.get("payload")
    if not payload_field:
        raise HTTPException(status_code=400, detail="missing payload")
    try:
        payload = json.loads(payload_field[0])
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="payload is not valid JSON")

    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(status_code=400, detail="no actions in payload")
    button = actions[0]
    button_id = button.get("action_id")
    action_id = button.get("value")
    if not action_id:
        raise HTTPException(status_code=400, detail="button missing action_id value")

    if button_id == "approve_action":
        decision = "approved"
    elif button_id == "deny_action":
        decision = "denied"
    else:
        raise HTTPException(status_code=400, detail=f"unknown button: {button_id}")

    user = payload.get("user") or {}
    user_name = (
        user.get("username")
        or user.get("name")
        or user.get("id")
        or "slack-user"
    )
    decided_by = f"slack:{user_name}"

    try:
        updated = _apply_approval(action_id, decision, decided_by)
    except HTTPException as e:
        # Already-decided or not-pending is fine — tell Slack the story without erroring.
        return JSONResponse({
            "response_type": "ephemeral",
            "replace_original": False,
            "text": f"⚠️ Could not apply decision: {e.detail}",
        })

    # Swap the original card to show the decision. Slack reads this response body.
    new_blocks = slack_decision_blocks(
        action_id=action_id,
        decision=decision,
        decided_by=user_name,
        decided_at=updated["decided_at"],
        prior_blocks=(payload.get("message") or {}).get("blocks"),
    )
    return {
        "replace_original": True,
        "text": f"Action {decision} by {user_name}",
        "blocks": new_blocks,
    }


@app.get("/policies")
def list_policies(project_id: Optional[str] = None):
    with db() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT id, created_at, rule_json, source, active, project_id FROM policies "
                "WHERE active=1 AND project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, rule_json, source, active, project_id FROM policies "
                "WHERE active=1 ORDER BY created_at DESC"
            ).fetchall()
    return [{**dict(r), "rule": json.loads(r["rule_json"])} for r in rows]


@app.post("/policies")
def add_policy(rule: dict, project_id: Optional[str] = None):
    err = validate_rule(rule)
    if err:
        raise HTTPException(status_code=400, detail=err)
    target = project_id or get_active_project_id()
    if not project_exists(target):
        raise HTTPException(status_code=400, detail=f"unknown project_id: {target}")
    pid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO policies (id, created_at, rule_json, source, project_id) VALUES (?, ?, ?, ?, ?)",
            (pid, now_iso(), json.dumps(rule), "manual", target),
        )
    return {"id": pid, "rule": rule, "project_id": target}


@app.post("/policies/from_text")
def policy_from_text(req: PolicyFromTextRequest):
    target = req.project_id or get_active_project_id()
    if not project_exists(target):
        raise HTTPException(status_code=400, detail=f"unknown project_id: {target}")
    rule = generate_policy_from_text(req.text)
    pid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO policies (id, created_at, rule_json, source, project_id) VALUES (?, ?, ?, ?, ?)",
            (pid, now_iso(), json.dumps(rule), "copilot", target),
        )
    return {"id": pid, "rule": rule, "source_text": req.text, "project_id": target}


@app.delete("/policies/{policy_id}")
def deactivate_policy(policy_id: str):
    with db() as conn:
        cur = conn.execute("UPDATE policies SET active=0 WHERE id=?", (policy_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="policy not found")
    return {"id": policy_id, "active": False}


# ---------- Projects ----------
@app.get("/projects")
def list_projects():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, created_at, active_flag FROM projects "
            "WHERE deleted=0 ORDER BY created_at ASC"
        ).fetchall()
        # Per-project policy + action counts.
        counts = {}
        for r in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM policies WHERE active=1 GROUP BY project_id"
        ).fetchall():
            counts.setdefault(r["project_id"], {})["policies"] = r["n"]
        for r in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM actions GROUP BY project_id"
        ).fetchall():
            counts.setdefault(r["project_id"], {})["actions"] = r["n"]
    projects = []
    for r in rows:
        d = dict(r)
        c = counts.get(d["id"], {})
        d["policy_count"] = c.get("policies", 0)
        d["action_count"] = c.get("actions", 0)
        d["active"] = bool(d.pop("active_flag"))
        projects.append(d)
    return projects


@app.post("/projects")
def create_project(req: ProjectCreateRequest):
    pid = req.id.strip().lower()
    if not pid or not pid.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(
            status_code=400,
            detail="project id must be alphanumeric (dashes and underscores allowed)",
        )
    with db() as conn:
        existing = conn.execute(
            "SELECT deleted FROM projects WHERE id=?", (pid,)
        ).fetchone()
        if existing and existing["deleted"] == 0:
            raise HTTPException(status_code=409, detail=f"project '{pid}' already exists")
        if existing:  # soft-deleted — undelete + update
            conn.execute(
                "UPDATE projects SET name=?, description=?, deleted=0 WHERE id=?",
                (req.name, req.description, pid),
            )
        else:
            conn.execute(
                "INSERT INTO projects (id, name, description, created_at, active_flag) VALUES (?, ?, ?, ?, 0)",
                (pid, req.name, req.description, now_iso()),
            )
        row = conn.execute(
            "SELECT id, name, description, created_at, active_flag FROM projects WHERE id=?", (pid,)
        ).fetchone()
    d = dict(row)
    d["active"] = bool(d.pop("active_flag"))
    return d


@app.post("/projects/{project_id}/activate")
def activate_project(project_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE id=? AND deleted=0", (project_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="project not found")
        conn.execute("UPDATE projects SET active_flag=0")
        conn.execute("UPDATE projects SET active_flag=1 WHERE id=?", (project_id,))
    return {"id": project_id, "active": True}


@app.delete("/projects/{project_id}")
def delete_project(project_id: str):
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="cannot delete the default project")
    with db() as conn:
        row = conn.execute(
            "SELECT active_flag FROM projects WHERE id=? AND deleted=0", (project_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="project not found")
        conn.execute("UPDATE projects SET deleted=1, active_flag=0 WHERE id=?", (project_id,))
        # If the deleted project was active, fall back to default.
        if row["active_flag"]:
            conn.execute("UPDATE projects SET active_flag=1 WHERE id=?", (DEFAULT_PROJECT_ID,))
    return {"id": project_id, "deleted": True}


# ---------- Learning ----------
@app.get("/learning")
def get_learning(project_id: Optional[str] = None):
    data = load_learning()
    if project_id:
        data = {
            **data,
            "signatures": {
                k: v for k, v in data.get("signatures", {}).items()
                if v.get("project_id") == project_id
            },
        }
    return data


@app.delete("/learning")
def reset_learning():
    save_learning({"version": 1, "signatures": {}})
    return {"ok": True, "cleared": True}


@app.delete("/learning/{key:path}")
def delete_learning_signature(key: str):
    data = load_learning()
    if key not in data.get("signatures", {}):
        raise HTTPException(status_code=404, detail="signature not found")
    data["signatures"].pop(key, None)
    save_learning(data)
    return {"ok": True, "removed": key}


# ---------- Live audience sessions ----------
def _serialize_session(sess: dict) -> dict:
    """Add derived fields the dashboard cares about."""
    out = dict(sess)
    out["seconds_remaining"] = session_seconds_remaining(sess)
    # Decode the stored actions JSON for convenience.
    if out.get("generated_actions"):
        try:
            out["generated_actions"] = json.loads(out["generated_actions"])
        except Exception:  # noqa: BLE001
            out["generated_actions"] = []
    # Count rules + executed actions in the session's project.
    with db() as conn:
        rule_row = conn.execute(
            "SELECT COUNT(*) AS n FROM policies WHERE active=1 AND project_id=?",
            (out["project_id"],),
        ).fetchone()
        act_row = conn.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE project_id=?",
            (out["project_id"],),
        ).fetchone()
    out["rule_count"] = rule_row["n"]
    out["action_count"] = act_row["n"]
    return out


@app.post("/sessions")
def create_session(req: Optional[SessionCreateRequest] = None):
    window = (req.window_seconds if req and req.window_seconds else SESSION_DEFAULT_WINDOW_S)
    window = max(5, min(window, 300))

    code = _generate_session_code()
    project_id = f"s{code}"
    project_name = f"Session {code}"
    now = now_iso()
    opened_until = (datetime.now(timezone.utc) + timedelta(seconds=window)).isoformat()

    with db() as conn:
        # Create the session-scoped project + activate it so the dashboard switches.
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, active_flag) VALUES (?, ?, ?, ?, 0)",
            (project_id, project_name, "Live audience-driven demo session.", now),
        )
        conn.execute("UPDATE projects SET active_flag=0")
        conn.execute("UPDATE projects SET active_flag=1 WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO sessions (code, project_id, created_at, opened_until, state) VALUES (?, ?, ?, ?, 'open')",
            (code, project_id, now, opened_until),
        )
        row = conn.execute("SELECT * FROM sessions WHERE code=?", (code,)).fetchone()

    return {**_serialize_session(dict(row)), "audience_path": f"/audience?code={code}"}


@app.get("/sessions")
def list_sessions():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    return [_serialize_session(dict(r)) for r in rows]


@app.get("/sessions/{code}")
def get_session_endpoint(code: str):
    sess = get_session(code)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    return _serialize_session(sess)


@app.post("/sessions/{code}/close")
def close_session(code: str):
    sess = get_session(code)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET state='closed', opened_until=? WHERE code=?",
            (now_iso(), code),
        )
        row = conn.execute("SELECT * FROM sessions WHERE code=?", (code,)).fetchone()
    return _serialize_session(dict(row))


@app.post("/sessions/{code}/generate")
def generate_session_agent(code: str):
    sess = get_session(code)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    rules = get_active_policies(project_id=sess["project_id"])
    actions = generate_adversarial_actions(rules)
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET state='agent_ready', generated_actions=? WHERE code=?",
            (json.dumps(actions), code),
        )
        row = conn.execute("SELECT * FROM sessions WHERE code=?", (code,)).fetchone()
    return _serialize_session(dict(row))


async def _fire_session_actions(code: str, project_id: str, actions: list) -> None:
    """Background runner: fires each action with a delay so it streams to the dashboard."""
    for i, action in enumerate(actions):
        req = ActionRequest(
            agent_id=f"audience-agent-{code}",
            task=f"Stress-test session {code}'s rules",
            action=action,
            project_id=project_id,
        )
        try:
            agent_action(req)  # synchronous DB write, fine from an async task
        except HTTPException as e:
            log.warning("session %s action %d failed: %s", code, i, e.detail)
        except Exception as e:  # noqa: BLE001
            log.warning("session %s action %d errored: %s", code, i, e)
        if i < len(actions) - 1:
            await asyncio.sleep(SESSION_RUN_DELAY_S)
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET state='completed', last_run_at=? WHERE code=?",
            (now_iso(), code),
        )


@app.post("/sessions/{code}/run")
async def run_session_agent(code: str):
    sess = get_session(code)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if not sess.get("generated_actions"):
        raise HTTPException(
            status_code=409,
            detail="no agent actions generated yet — call /sessions/{code}/generate first",
        )
    try:
        actions = json.loads(sess["generated_actions"])
    except Exception:
        raise HTTPException(status_code=500, detail="stored actions are not valid JSON")

    with db() as conn:
        conn.execute("UPDATE sessions SET state='running' WHERE code=?", (code,))

    asyncio.create_task(_fire_session_actions(code, sess["project_id"], actions))
    return {
        "ok": True,
        "code": code,
        "actions_queued": len(actions),
        "estimated_duration_s": round(SESSION_RUN_DELAY_S * (len(actions) - 1), 1),
    }


# ---------- Canonical demo agent (replaces running demo_agent.py from terminal) ----------
async def _fire_demo_actions(project_id: str) -> None:
    """Background runner for /demo/run. Streams the canonical 8 FALLBACK_ACTIONS
    through the policy engine with a 1.2s delay between each so they appear live on
    the dashboard — same behavior as demo_agent.py, but no terminal required."""
    for i, action in enumerate(FALLBACK_ACTIONS):
        req = ActionRequest(
            agent_id="devops-agent-prod-01",
            task="Provision dev environment for customer demo next week",
            action=action,
            project_id=project_id,
        )
        try:
            agent_action(req)
        except HTTPException as e:
            log.warning("demo action %d failed: %s", i, e.detail)
        except Exception as e:  # noqa: BLE001
            log.warning("demo action %d errored: %s", i, e)
        if i < len(FALLBACK_ACTIONS) - 1:
            await asyncio.sleep(SESSION_RUN_DELAY_S)


@app.post("/demo/run")
async def run_demo_agent(project_id: Optional[str] = None):
    """Fire the canonical 8-action demo (storage, db, vm, lb, the $48K H100 trap, ...)
    through the policy engine in the background. Call this from the dashboard button
    so you don't have to alt-tab to a terminal during the stage demo."""
    target = project_id or get_active_project_id()
    if not project_exists(target):
        raise HTTPException(status_code=400, detail=f"unknown project_id: {target}")
    asyncio.create_task(_fire_demo_actions(target))
    return {
        "ok": True,
        "project_id": target,
        "actions_queued": len(FALLBACK_ACTIONS),
        "estimated_duration_s": round(SESSION_RUN_DELAY_S * (len(FALLBACK_ACTIONS) - 1), 1),
    }


@app.delete("/sessions/{code}")
def delete_session(code: str):
    sess = get_session(code)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE code=?", (code,))
        # Soft-delete the associated project and fall back to default if it was active.
        was_active = conn.execute(
            "SELECT active_flag FROM projects WHERE id=?", (sess["project_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE projects SET deleted=1, active_flag=0 WHERE id=?",
            (sess["project_id"],),
        )
        if was_active and was_active["active_flag"]:
            conn.execute(
                "UPDATE projects SET active_flag=1 WHERE id=?",
                (DEFAULT_PROJECT_ID,),
            )
    return {"ok": True, "code": code, "deleted": True}


# ---------- Audience submission ----------
@app.post("/audience/submit")
def audience_submit(req: AudienceSubmitRequest, request: Request):
    sess = get_session(req.code.strip())
    if not sess:
        raise HTTPException(status_code=404, detail="session code not found")
    if sess["state"] != "open":
        raise HTTPException(
            status_code=409,
            detail=f"session is {sess['state']} — submission window closed",
        )
    if session_seconds_remaining(sess) <= 0:
        with db() as conn:
            conn.execute("UPDATE sessions SET state='closed' WHERE code=?", (sess["code"],))
        raise HTTPException(status_code=409, detail="submission window closed")

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="rule text is empty")
    if len(text) > 280:
        raise HTTPException(status_code=400, detail="rule text too long (max 280 chars)")

    # Per-IP throttle (in-memory, best effort).
    client_ip = request.client.host if request.client else "unknown"
    now_ts = datetime.now(timezone.utc).timestamp()
    last = _audience_last_submit.get(client_ip, 0)
    if now_ts - last < AUDIENCE_THROTTLE_S:
        raise HTTPException(
            status_code=429,
            detail=f"slow down — wait {AUDIENCE_THROTTLE_S:.0f}s between submissions",
        )
    _audience_last_submit[client_ip] = now_ts

    rule = generate_policy_from_text(text)
    rule.setdefault("source_text", text)
    pid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO policies (id, created_at, rule_json, source, project_id) VALUES (?, ?, ?, ?, ?)",
            (pid, now_iso(), json.dumps(rule), "audience", sess["project_id"]),
        )
    return {
        "ok": True,
        "code": sess["code"],
        "rule": rule,
        "rules_in_session": _serialize_session(get_session(sess["code"]))["rule_count"],
    }


@app.post("/admin/reset")
def admin_reset():
    """Restore the dashboard to its first-version state: close any audience sessions,
    soft-delete session projects, drop any user-added rules from the default project
    (keeping the seeded defaults), and reactivate the default project.

    Preserves: audit log, learning store, the seeded default policies.
    Removes: active sessions and their projects, copilot/audience/manual rules in default."""
    closed_sessions = 0
    deleted_projects = 0
    removed_policies = 0
    with db() as conn:
        # Close every session that's not already closed/completed and soft-delete its project.
        sess_rows = conn.execute(
            "SELECT code, project_id FROM sessions WHERE state NOT IN ('closed','completed')"
        ).fetchall()
        for s in sess_rows:
            conn.execute(
                "UPDATE sessions SET state='closed', opened_until=? WHERE code=?",
                (now_iso(), s["code"]),
            )
            closed_sessions += 1

        # Soft-delete every non-default project so the switcher and active flag clear up.
        proj_rows = conn.execute(
            "SELECT id FROM projects WHERE id != ? AND deleted=0",
            (DEFAULT_PROJECT_ID,),
        ).fetchall()
        for p in proj_rows:
            conn.execute(
                "UPDATE projects SET deleted=1, active_flag=0 WHERE id=?", (p["id"],)
            )
            deleted_projects += 1

        # Remove non-seed policies from the default project (keep the originals).
        cur = conn.execute(
            "DELETE FROM policies WHERE project_id=? AND source != 'default'",
            (DEFAULT_PROJECT_ID,),
        )
        removed_policies = cur.rowcount or 0

        # Make sure default is active.
        conn.execute("UPDATE projects SET active_flag=0")
        conn.execute(
            "UPDATE projects SET active_flag=1 WHERE id=? AND deleted=0",
            (DEFAULT_PROJECT_ID,),
        )

    # Re-run init to refill any missing seed policies (idempotent).
    init_db()

    return {
        "ok": True,
        "closed_sessions": closed_sessions,
        "deleted_projects": deleted_projects,
        "removed_user_policies": removed_policies,
        "active_project": get_active_project_id(),
        "message": "Reset complete — default project active, seeded policies restored.",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini": _gemini_ready,
        "slack": bool(SLACK_WEBHOOK_URL),
        "db": DB_PATH,
        "learning": LEARNING_PATH,
        "active_project": get_active_project_id(),
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
  .badge.learned { background: rgba(139,92,246,0.12); color: #a78bfa; border: 1px solid rgba(139,92,246,0.30); }
  .badge.project { background: rgba(59,130,246,0.10); color: var(--blue); border: 1px solid rgba(59,130,246,0.25); }
  .row.learned { border-left-color: #a78bfa; }
  select.project-select { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); font-size: 13px; padding: 6px 10px; border-radius: 4px; outline: none; cursor: pointer; }
  select.project-select:hover { background: #1a1a1a; }
  select.project-select:focus { border-color: var(--blue); }
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

<header class="border-b border-[var(--border)] px-6 py-4 flex items-center justify-between gap-4 flex-wrap">
  <div class="flex items-center gap-3">
    <div class="mono text-lg font-semibold tracking-tight">LEDGER</div>
    <div class="text-[11px] text-[var(--muted)] uppercase tracking-widest">Policy & Evidence Layer for Agent Spend</div>
  </div>
  <div class="flex items-center gap-3 ml-auto">
    <span class="text-[10px] text-[var(--muted)] uppercase tracking-widest">Project</span>
    <select id="project-switcher" class="project-select"></select>
    <button id="project-new" class="btn" title="Create a new project">+ new</button>
    <button id="admin-reset" class="btn btn-danger" title="Close any audience session, switch back to the default project, and restore the original seeded policies. Audit log and learning are preserved.">↺ Reset to defaults</button>
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
        <button id="demo-run" class="btn btn-primary" title="Fire the canonical 8-action demo (includes the $48K H100 trap)">▶ Run demo agent</button>
        <span><span class="badge approved">approved</span></span>
        <span><span class="badge pending_approval">pending</span></span>
        <span><span class="badge blocked">blocked</span></span>
      </div>
    </div>
    <div id="actions" class="scrollbar overflow-auto max-h-[78vh]"></div>
    <div id="empty" class="px-6 py-12 text-center text-sm text-[var(--muted)] hidden">
      Waiting for agent actions…<br>
      <span class="text-xs">Click <span class="text-[var(--green)]">▶ Run demo agent</span> above to start the demo</span>
    </div>
  </section>

  <!-- Right column -->
  <section class="col-span-12 lg:col-span-4 flex flex-col gap-4">
    <!-- Live Session -->
    <div class="panel">
      <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <div class="flex items-center gap-2">
          <h2 class="text-sm font-semibold uppercase tracking-widest">Live Session</h2>
          <span id="session-state-badge" class="badge" style="display:none"></span>
        </div>
        <span id="session-meta" class="text-xs text-[var(--muted)] mono"></span>
      </div>
      <div id="session-body" class="p-4 flex flex-col gap-3"></div>
    </div>

    <!-- Policies -->
    <div class="panel">
      <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <h2 class="text-sm font-semibold uppercase tracking-widest">Active Policies</h2>
        <span id="policy-count" class="text-xs text-[var(--muted)] mono"></span>
      </div>
      <div id="policies" class="scrollbar overflow-auto max-h-[30vh] p-3 flex flex-col gap-2"></div>
    </div>

    <!-- Learned Patterns -->
    <div class="panel">
      <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <div class="flex items-center gap-2">
          <h2 class="text-sm font-semibold uppercase tracking-widest">Learned Patterns</h2>
          <span class="badge learned">RL</span>
        </div>
        <div class="flex items-center gap-2">
          <span id="learning-count" class="text-xs text-[var(--muted)] mono"></span>
          <button id="learning-reset" class="btn" title="Reset all learning (clears learning.json)">reset</button>
        </div>
      </div>
      <div id="learning" class="scrollbar overflow-auto max-h-[22vh] p-3 flex flex-col gap-2"></div>
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
          <span id="copilot-status" class="text-xs text-[var(--muted)]">Adds to <span id="copilot-target" class="mono"></span> · Enter to generate</span>
          <button id="copilot-submit" class="btn btn-primary">Generate rule</button>
        </div>
        <div id="copilot-result" class="mt-3 hidden"></div>
      </div>
    </div>
  </section>
</main>

<script>
const ENDPOINTS = {
  audit: (pid) => `/audit?limit=50&project_id=${encodeURIComponent(pid)}`,
  policies: (pid) => `/policies?project_id=${encodeURIComponent(pid)}`,
  learning: (pid) => `/learning?project_id=${encodeURIComponent(pid)}`,
  learningReset: '/learning',
  fromText: '/policies/from_text',
  approve: (id) => `/approve/${id}`,
  projects: '/projects',
  activate: (pid) => `/projects/${encodeURIComponent(pid)}/activate`,
  createProject: '/projects',
  health: '/health',
  sessions: '/sessions',
  newSession: '/sessions',
  closeSession: (code) => `/sessions/${code}/close`,
  generateAgent: (code) => `/sessions/${code}/generate`,
  runAgent: (code) => `/sessions/${code}/run`,
  runDemo: '/demo/run',
  adminReset: '/admin/reset',
};

const state = {
  actions: [],
  policies: [],
  learning: {},
  projects: [],
  activeProject: 'default',
  expanded: new Set(),
  session: null,
};

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

function isLearned(action) {
  return (action.rule_violated || '').startsWith('learned_pattern');
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
    const learned = isLearned(a);
    const rowClass = learned ? 'learned' : a.verdict;
    return `
      <div class="row ${rowClass} px-4 py-3 border-b border-[var(--border)] cursor-pointer fade-in" data-id="${a.id}">
        <div class="flex items-center gap-3 text-sm">
          <span class="mono text-[var(--muted)] text-xs w-16">${timeShort(a.timestamp)}</span>
          <span class="mono text-[var(--muted)] text-xs w-40 truncate" title="${escapeHtml(a.agent_id)}">${escapeHtml(a.agent_id)}</span>
          <span class="flex-1 truncate">${escapeHtml(desc)}</span>
          <span class="mono text-xs text-right w-24">${escapeHtml(fmtMoney(cost))}</span>
          ${badge(a.verdict)}
          ${learned ? '<span class="badge learned" title="Auto-approved from learned history">learned</span>' : ''}
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
  document.getElementById('policy-count').textContent =
    state.policies.length + ' active · ' + state.activeProject;
  if (!state.policies.length) {
    root.innerHTML = `<div class="text-xs text-[var(--muted)] px-2 py-4">No policies active in <span class="mono">${escapeHtml(state.activeProject)}</span>.</div>`;
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

function renderProjects() {
  const sel = document.getElementById('project-switcher');
  const opts = state.projects.map(p => {
    const lbl = `${p.name} (${p.id}) — ${p.policy_count} rules, ${p.action_count} actions`;
    return `<option value="${escapeHtml(p.id)}" ${p.id === state.activeProject ? 'selected' : ''}>${escapeHtml(lbl)}</option>`;
  }).join('');
  if (sel.innerHTML !== opts) sel.innerHTML = opts;
  document.getElementById('copilot-target').textContent = state.activeProject;
}

function renderLearning() {
  const root = document.getElementById('learning');
  const sigs = Object.entries(state.learning.signatures || {});
  document.getElementById('learning-count').textContent =
    sigs.length + (sigs.length === 1 ? ' pattern' : ' patterns');
  if (!sigs.length) {
    root.innerHTML = `<div class="text-xs text-[var(--muted)] px-2 py-4">No learned patterns yet. Approve or deny pending actions and they will appear here. ${LEARN_MIN_APPROVALS_HINT}</div>`;
    return;
  }
  // Sort by approved_count descending.
  sigs.sort((a, b) => (b[1].approved_count - a[1].approved_count) || (b[1].denied_count - a[1].denied_count));
  root.innerHTML = sigs.map(([key, s]) => {
    const ready = (s.denied_count === 0 && s.approved_count >= 3);
    return `
      <div class="panel-2 px-3 py-2">
        <div class="flex items-center justify-between gap-2">
          <div class="mono text-[11px] truncate" title="${escapeHtml(key)}">${escapeHtml(key)}</div>
          ${ready ? '<span class="badge learned" title="Will auto-approve similar actions">auto</span>' : ''}
        </div>
        <div class="flex items-center gap-3 text-xs mt-1">
          <span class="text-[var(--green)]">✓ ${s.approved_count}</span>
          <span class="text-[var(--red)]">✗ ${s.denied_count}</span>
          ${s.min_approved_cost != null ? `<span class="text-[var(--muted)] mono">range $${Number(s.min_approved_cost).toLocaleString()}–$${Number(s.max_approved_cost).toLocaleString()}</span>` : ''}
        </div>
      </div>`;
  }).join('');
}
const LEARN_MIN_APPROVALS_HINT = '<span class="mono">3+ approvals · 0 denials → auto</span>';

function audienceUrlFor(code) {
  return `${location.origin}/audience?code=${code}`;
}

function qrSrc(code) {
  const url = encodeURIComponent(audienceUrlFor(code));
  return `https://api.qrserver.com/v1/create-qr-code/?size=180x180&margin=8&data=${url}`;
}

function renderSession() {
  const body = document.getElementById('session-body');
  const meta = document.getElementById('session-meta');
  const badge = document.getElementById('session-state-badge');
  const s = state.session;

  if (!s) {
    badge.style.display = 'none';
    meta.textContent = '';
    body.innerHTML = `
      <div class="text-sm text-[var(--muted)]">
        No live session. Start one to let the audience write rules from their phones.
      </div>
      <button id="session-new" class="btn btn-primary" style="align-self:flex-start">Start new session</button>
    `;
    document.getElementById('session-new').onclick = onNewSession;
    return;
  }

  const stateColor = {
    open: 'pending_approval', closed: 'pending_approval',
    agent_ready: 'project',   running: 'project',
    completed: 'approved',
  }[s.state] || 'pending_approval';
  badge.className = 'badge ' + stateColor;
  badge.textContent = s.state.replace('_', ' ');
  badge.style.display = '';
  meta.textContent = `${s.rule_count} rules · ${s.action_count} actions`;

  const url = audienceUrlFor(s.code);
  const isOpen = s.state === 'open' && s.seconds_remaining > 0;
  const showRun  = s.state === 'agent_ready';
  const showGen  = s.state === 'closed' || (s.state === 'open' && s.rule_count > 0);
  const showNew  = ['completed','running','agent_ready','closed'].includes(s.state);

  body.innerHTML = `
    <div class="flex items-start gap-4">
      <div class="flex-1">
        <div class="mono text-[10px] uppercase tracking-widest text-[var(--muted)] mb-1">Session code</div>
        <div class="mono text-4xl tracking-[0.3em] leading-none">${escapeHtml(s.code)}</div>
        <div class="mono text-[11px] text-[var(--muted)] mt-2 break-all">${escapeHtml(url)}</div>
        ${isOpen ? `<div class="text-xs text-[var(--yellow)] mt-2"><b>${s.seconds_remaining}s</b> remaining</div>` : ''}
      </div>
      <img src="${qrSrc(s.code)}" alt="QR" class="rounded border border-[var(--border)] bg-white" width="100" height="100" />
    </div>

    ${s.generated_actions ? `
      <div class="panel-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-widest text-[var(--muted)] mb-2">Generated adversarial agent</div>
        <ol class="text-xs space-y-1">
          ${s.generated_actions.map(a => `
            <li class="flex items-center gap-2">
              <span class="mono text-[var(--muted)] w-24 truncate">${escapeHtml(a.category || '')}</span>
              <span class="flex-1 truncate">${escapeHtml(a.description || '')}</span>
              <span class="mono">${escapeHtml(fmtMoney(a.cost_usd ?? 0))}</span>
            </li>
          `).join('')}
        </ol>
      </div>` : ''}

    <div class="flex flex-wrap items-center gap-2">
      ${isOpen   ? '<button id="session-close" class="btn">Close window</button>' : ''}
      ${showGen  ? '<button id="session-gen"   class="btn">Generate adversarial agent</button>' : ''}
      ${showRun  ? '<button id="session-run"   class="btn btn-primary">Run agent</button>' : ''}
      ${showNew  ? '<button id="session-new"   class="btn">Start new session</button>' : ''}
    </div>
  `;

  document.getElementById('session-close')?.addEventListener('click', onCloseSession);
  document.getElementById('session-gen')?.addEventListener('click', onGenerateAgent);
  document.getElementById('session-run')?.addEventListener('click', onRunAgent);
  document.getElementById('session-new')?.addEventListener('click', onNewSession);
}

async function onNewSession() {
  try {
    const s = await fetchJSON(ENDPOINTS.newSession, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    state.session = s;
    state.activeProject = s.project_id;
    state.expanded.clear();
    await poll();
  } catch (e) { alert('Failed to start session: ' + e.message); }
}
async function onCloseSession() {
  if (!state.session) return;
  try { await fetchJSON(ENDPOINTS.closeSession(state.session.code), { method: 'POST' }); await poll(); }
  catch (e) { alert('Failed: ' + e.message); }
}
async function onGenerateAgent() {
  if (!state.session) return;
  const btn = document.getElementById('session-gen');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating with Gemini…'; }
  try { await fetchJSON(ENDPOINTS.generateAgent(state.session.code), { method: 'POST' }); await poll(); }
  catch (e) { alert('Failed: ' + e.message); }
}
async function onRunAgent() {
  if (!state.session) return;
  try { await fetchJSON(ENDPOINTS.runAgent(state.session.code), { method: 'POST' }); await poll(); }
  catch (e) { alert('Failed: ' + e.message); }
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function poll() {
  try {
    const [projects, sessions] = await Promise.all([
      fetchJSON(ENDPOINTS.projects),
      fetchJSON(ENDPOINTS.sessions),
    ]);
    state.projects = projects;
    const active = projects.find(p => p.active);
    if (active) state.activeProject = active.id;
    // Most recent session is the "current" one. May be null.
    state.session = sessions.length ? sessions[0] : null;
    const pid = state.activeProject;
    const [acts, pols, learn] = await Promise.all([
      fetchJSON(ENDPOINTS.audit(pid)),
      fetchJSON(ENDPOINTS.policies(pid)),
      fetchJSON(ENDPOINTS.learning(pid)),
    ]);
    state.actions = acts;
    state.policies = pols;
    state.learning = learn || { signatures: {} };
    renderProjects();
    renderActions();
    renderPolicies();
    renderLearning();
    renderSession();
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

// Run demo agent (fires the canonical 8 actions through the policy engine, server-side)
const demoBtn = document.getElementById('demo-run');
demoBtn.addEventListener('click', async () => {
  const originalText = demoBtn.textContent;
  demoBtn.disabled = true;
  demoBtn.textContent = 'Firing actions…';
  try {
    const out = await fetchJSON(ENDPOINTS.runDemo, { method: 'POST' });
    const total = (out.estimated_duration_s || 12) * 1000 + 1500;
    // Poll faster while the demo streams so the UI keeps up.
    await poll();
    setTimeout(() => {
      demoBtn.disabled = false;
      demoBtn.textContent = originalText;
    }, total);
  } catch (e) {
    demoBtn.disabled = false;
    demoBtn.textContent = originalText;
    alert('Failed to start demo: ' + e.message);
  }
});

// Project switcher
document.getElementById('project-switcher').addEventListener('change', async (ev) => {
  const pid = ev.target.value;
  try {
    await fetchJSON(ENDPOINTS.activate(pid), { method: 'POST' });
    state.activeProject = pid;
    state.expanded.clear();
    await poll();
  } catch (e) { alert('Failed to switch: ' + e.message); }
});

document.getElementById('admin-reset').addEventListener('click', async () => {
  const ok = confirm(
    'Reset to defaults?\n\n' +
    '• Any audience session will be closed\n' +
    '• You will return to the default project\n' +
    '• Rules added via Policy Copilot or audience submissions will be removed\n' +
    '• Audit log and learning history are preserved\n\n' +
    'Continue?'
  );
  if (!ok) return;
  const btn = document.getElementById('admin-reset');
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Resetting…';
  try {
    const out = await fetchJSON(ENDPOINTS.adminReset, { method: 'POST' });
    state.activeProject = out.active_project || 'default';
    state.expanded.clear();
    state.session = null;
    await poll();
    btn.textContent = `Reset ✓  (closed ${out.closed_sessions}, removed ${out.removed_user_policies} rules)`;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
  } catch (e) {
    btn.disabled = false; btn.textContent = orig;
    alert('Reset failed: ' + e.message);
  }
});

document.getElementById('project-new').addEventListener('click', async () => {
  const id = prompt('Project id (alphanumeric, dashes/underscores allowed):');
  if (!id) return;
  const name = prompt('Project display name:', id) || id;
  try {
    await fetchJSON(ENDPOINTS.createProject, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, name }),
    });
    await fetchJSON(ENDPOINTS.activate(id.trim().toLowerCase()), { method: 'POST' });
    await poll();
  } catch (e) { alert('Failed: ' + e.message); }
});

document.getElementById('learning-reset').addEventListener('click', async () => {
  if (!confirm('Wipe all learned patterns? This clears learning.json across every project.')) return;
  try {
    await fetchJSON(ENDPOINTS.learningReset, { method: 'DELETE' });
    await poll();
  } catch (e) { alert('Failed: ' + e.message); }
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


# ---------- Audience page ----------
AUDIENCE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no" />
<title>Write a rule · Ledger demo</title>
<style>
  :root {
    --bg: #0a0a0a; --panel: #141414; --border: #1f1f1f;
    --text: #e5e5e5; --muted: #8a8a8a;
    --green: #10b981; --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6;
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body { background: var(--bg); color: var(--text); margin: 0; min-height: 100vh; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding: 24px 18px 48px;
    display: flex; flex-direction: column; align-items: stretch; gap: 16px;
    max-width: 460px; margin: 0 auto;
  }
  .mono { font-family: ui-monospace, "SF Mono", "Cascadia Code", monospace; }
  .brand { font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }
  h1 { font-size: 22px; margin: 6px 0 4px; line-height: 1.2; font-weight: 600; }
  p.intro { color: var(--muted); font-size: 14px; line-height: 1.4; margin: 0 0 6px; }
  label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); margin-bottom: 6px; }
  input[type="text"], textarea {
    width: 100%;
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 12px; font-size: 16px;
    outline: none;
  }
  input[type="text"]:focus, textarea:focus { border-color: var(--blue); }
  textarea { resize: vertical; min-height: 120px; font-family: inherit; }
  #code { font-family: ui-monospace, "SF Mono", monospace; letter-spacing: 0.5em; font-size: 28px; text-align: center; padding: 16px 12px; }
  button {
    width: 100%; padding: 14px 16px; border-radius: 6px; border: 0;
    background: var(--green); color: #00120b; font-size: 16px; font-weight: 600; cursor: pointer;
    transition: background 100ms ease;
  }
  button:active { background: #0a8f63; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .status { font-size: 13px; padding: 12px 14px; border-radius: 6px; border: 1px solid var(--border); background: var(--panel); display: none; }
  .status.ok { display: block; border-color: rgba(16,185,129,0.35); color: var(--green); background: rgba(16,185,129,0.08); }
  .status.err { display: block; border-color: rgba(239,68,68,0.35); color: var(--red); background: rgba(239,68,68,0.08); }
  .status.info { display: block; border-color: rgba(59,130,246,0.35); color: var(--blue); background: rgba(59,130,246,0.08); }
  .meta { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .rule-preview { font-family: ui-monospace, monospace; font-size: 11px; white-space: pre-wrap; word-break: break-word; margin-top: 8px; color: var(--text); }
  .footer { text-align: center; font-size: 11px; color: var(--muted); margin-top: 12px; }
</style>
</head>
<body>
  <div class="brand">Ledger · Live demo</div>
  <h1>Write a rule for our AI agent</h1>
  <p class="intro">
    Type a governance rule in plain English — anything like
    <span class="mono">"no spend over $200"</span> or
    <span class="mono">"block payments to crypto vendors"</span>.
    We will turn it into a policy. After the timer ends, an AI agent will try to break it on stage.
  </p>

  <div>
    <label for="code">Session code</label>
    <input type="text" id="code" inputmode="numeric" maxlength="4" autocomplete="off" placeholder="0000" />
  </div>

  <div>
    <label for="rule">Your rule</label>
    <textarea id="rule" maxlength="280" placeholder="No spending over $500. No GPU on weekends. Block payments to crypto vendors. …"></textarea>
    <div class="meta"><span id="counter">0</span> / 280</div>
  </div>

  <button id="submit">Submit rule</button>
  <div id="status" class="status"></div>

  <div class="footer">Submissions are anonymous · one rule per ~5 seconds</div>

<script>
const params = new URLSearchParams(location.search);
const codeInput = document.getElementById('code');
const ruleInput = document.getElementById('rule');
const submitBtn = document.getElementById('submit');
const statusEl = document.getElementById('status');
const counter = document.getElementById('counter');

if (params.get('code')) codeInput.value = params.get('code').slice(0,4);

ruleInput.addEventListener('input', () => { counter.textContent = ruleInput.value.length; });
codeInput.addEventListener('input', () => {
  codeInput.value = codeInput.value.replace(/\D/g, '').slice(0,4);
});

function show(kind, msg, preview) {
  statusEl.className = 'status ' + kind;
  statusEl.innerHTML = msg + (preview ? `<div class="rule-preview">${preview}</div>` : '');
}

submitBtn.addEventListener('click', async () => {
  const code = codeInput.value.trim();
  const text = ruleInput.value.trim();
  if (code.length !== 4) { show('err', 'Session code must be 4 digits.'); return; }
  if (text.length < 3)  { show('err', 'Type a rule first.'); return; }
  submitBtn.disabled = true;
  show('info', 'Generating policy…');
  try {
    const r = await fetch('/audience/submit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code, text}),
    });
    const out = await r.json();
    if (!r.ok) {
      const detail = typeof out.detail === 'string' ? out.detail : JSON.stringify(out.detail);
      show('err', detail || `HTTP ${r.status}`);
    } else {
      const desc = (out.rule && out.rule.description) || '(rule added)';
      const escape = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      show('ok', `✓ Rule #${out.rules_in_session} added to session <b>${escape(out.code)}</b>.`, escape(desc));
      ruleInput.value = '';
      counter.textContent = '0';
    }
  } catch (e) {
    show('err', 'Network error: ' + e.message);
  } finally {
    submitBtn.disabled = false;
  }
});
</script>
</body>
</html>
"""


@app.get("/audience", response_class=HTMLResponse)
def audience_page():
    return HTMLResponse(AUDIENCE_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
    )
