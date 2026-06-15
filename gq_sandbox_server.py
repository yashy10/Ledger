"""
gq_sandbox_server.py — A mock "Great Question" research MCP server (stdio).

This stands in for Great Question's real MCP surface so the gateway demo is fully
self-contained and SAFE: the dangerous write tools (recruit, pay incentives,
export PII, bulk-email) return canned results and have ZERO real side effects.

When the real GQ MCP server is wired into the gateway, its tools take precedence
and these are only used to fill gaps (e.g. to keep the escalation beats working
even if the real surface is read-only). See ledger_gateway.py.

It is a downstream server: the gateway spawns it and connects as an MCP client.
stdout is the JSON-RPC channel — all logging goes to stderr, and we never print().

Run standalone (for a quick sanity check, it just waits on stdio):
    python3 gq_sandbox_server.py
"""
import json
import logging
import sys

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [gq-sandbox] %(message)s")
log = logging.getLogger("gq-sandbox")

server = Server("great-question-sandbox")

# ---- canned data -------------------------------------------------------------
STUDIES = [
    {"id": "stu_001", "title": "Enterprise onboarding churn", "status": "live", "responses": 84},
    {"id": "stu_002", "title": "Pricing page comprehension", "status": "live", "responses": 41},
    {"id": "stu_003", "title": "Mobile dashboard usability", "status": "draft", "responses": 0},
]
INSIGHTS = {
    "stu_001": {
        "themes": [
            {"theme": "Onboarding friction (too many setup steps)", "share": 0.42},
            {"theme": "Unclear value in first session", "share": 0.27},
            {"theme": "Missing integrations (SSO, SCIM)", "share": 0.18},
        ],
        "verbatims": [
            "I never got past connecting our data sources.",
            "It wasn't obvious what to do after signing up.",
        ],
    }
}
FAKE_PII = [
    {"name": "Dana Whitfield", "email": "dana.whitfield@northwind-corp.com", "company": "Northwind"},
    {"name": "Marcus Lee", "email": "marcus.lee@brightford.io", "company": "Brightford"},
    {"name": "Priya Anand", "email": "priya.anand@summit-health.org", "company": "Summit Health"},
]


# ---- tool definitions --------------------------------------------------------
TOOLS = [
    types.Tool(
        name="list_studies",
        description="List research studies in the workspace.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="search_responses",
        description="Search participant responses across studies.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "study_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="get_insights",
        description="Get synthesized themes and verbatims for a study.",
        inputSchema={
            "type": "object",
            "properties": {"study_id": {"type": "string"}},
            "required": ["study_id"],
        },
    ),
    types.Tool(
        name="create_study",
        description="Create a new draft research study.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="recruit_participants",
        description="Recruit participants matching a segment into a study.",
        inputSchema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "How many participants to recruit"},
                "segment": {"type": "string", "description": "Audience / targeting criteria"},
                "study_id": {"type": "string"},
            },
            "required": ["count", "segment"],
        },
    ),
    types.Tool(
        name="send_incentive",
        description="Pay cash incentives to participants.",
        inputSchema={
            "type": "object",
            "properties": {
                "participant_ids": {"type": "array", "items": {"type": "string"}},
                "amount_usd": {"type": "number", "description": "Per-participant incentive in USD"},
            },
            "required": ["amount_usd"],
        },
    ),
    types.Tool(
        name="export_participant_data",
        description="Export participant records (may include PII such as name and email).",
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "count": {"type": "integer"},
            },
            "required": ["study_id", "fields"],
        },
    ),
    types.Tool(
        name="send_bulk_message",
        description="Send a message to many participants at once.",
        inputSchema={
            "type": "object",
            "properties": {
                "segment": {"type": "string"},
                "recipients": {"type": "integer", "description": "Number of recipients"},
                "body": {"type": "string"},
            },
            "required": ["recipients", "body"],
        },
    ),
]


# ---- handlers (canned, no side effects) --------------------------------------
def h_list_studies(a):
    return {"studies": STUDIES}


def h_search_responses(a):
    limit = int(a.get("limit", 5) or 5)
    verbatims = [
        {"participant": "p_2231", "sentiment": "negative", "text": "Setup took forever and I gave up."},
        {"participant": "p_8842", "sentiment": "neutral", "text": "It's fine once configured."},
        {"participant": "p_5190", "sentiment": "negative", "text": "Couldn't connect SSO, blocked our rollout."},
    ]
    return {"query": a.get("query"), "results": verbatims[:limit]}


def h_get_insights(a):
    return INSIGHTS.get(a.get("study_id"), {"themes": [], "verbatims": [], "note": "no insights for study"})


def h_create_study(a):
    return {"study_id": "stu_new_5567", "title": a.get("title"), "status": "draft"}


def h_recruit_participants(a):
    count = int(a.get("count", 0) or 0)
    return {
        "recruited": count,
        "segment": a.get("segment"),
        "estimated_completion": "3 days",
        "status": "scheduled",
    }


def h_send_incentive(a):
    ids = a.get("participant_ids") or []
    n = len(ids) if ids else 1
    amount = float(a.get("amount_usd", 0) or 0)
    return {"paid": n, "amount_usd": amount, "total_usd": round(amount * n, 2), "status": "sent"}


def h_export_participant_data(a):
    fields = a.get("fields") or ["name", "email"]
    rows = [{k: r.get(k, "") for k in fields} for r in FAKE_PII]
    return {
        "study_id": a.get("study_id"),
        "row_count": int(a.get("count", len(rows)) or len(rows)),
        "fields": fields,
        "preview": rows,
    }


def h_send_bulk_message(a):
    return {"queued": int(a.get("recipients", 0) or 0), "segment": a.get("segment"), "status": "queued"}


HANDLERS = {
    "list_studies": h_list_studies,
    "search_responses": h_search_responses,
    "get_insights": h_get_insights,
    "create_study": h_create_study,
    "recruit_participants": h_recruit_participants,
    "send_incentive": h_send_incentive,
    "export_participant_data": h_export_participant_data,
    "send_bulk_message": h_send_bulk_message,
}


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    handler = HANDLERS.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
    result = handler(arguments or {})
    log.info("executed %s", name)
    return [types.TextContent(type="text", text=json.dumps(result))]


async def main():
    log.info("starting; %d tools", len(TOOLS))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
