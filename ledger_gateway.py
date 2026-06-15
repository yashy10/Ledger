"""
ledger_gateway.py — An MCP gateway that puts Ledger in the path of an agent's
MCP tool calls.

It is simultaneously:
  • an MCP SERVER (over stdio) that an MCP client — Claude Desktop, Cursor, or
    mcp_demo_client.py — connects to, and
  • an MCP CLIENT to one or more downstream MCP servers (Great Question's real
    server, and/or the local gq_sandbox_server.py).

For every tool call the agent makes, the gateway:
  1. maps (tool, args) → a Ledger action          (gq_tool_map.py)
  2. POSTs it to Ledger /agent/action             (policy + Slack + SHA-256 audit;
                                                    appears live on the dashboard)
  3. approved        → forwards to the downstream and returns the result
     pending_approval→ blocks, polling /audit/{id}.human_decision until a human
                       approves/denies in Slack or the dashboard
                       (FAIL-SAFE: on denied OR timeout the tool is NOT executed)

Ledger's own main.py is never imported or modified — this is a pure HTTP client
of the existing, running server.

stdout is the JSON-RPC channel; ALL logging goes to stderr and we never print().

Config (env):
  LEDGER_URL=http://localhost:8000     LEDGER_PROJECT_ID=great-question
  GATEWAY_AGENT_ID=gq-research-agent
  GQ_MCP_URL=...                       # real Great Question MCP server (optional)
  GQ_MCP_TOKEN=...                     # bearer token for the real server
  GQ_MCP_TRANSPORT=http|sse|stdio      # default http
  GQ_MCP_CMD / GQ_MCP_ARGS             # for GQ_MCP_TRANSPORT=stdio
  SANDBOX_TOOLS=on|off                 # default on (mounts gq_sandbox_server.py)
  APPROVAL_TIMEOUT_S=120  APPROVAL_POLL_S=1.0  HTTP_TIMEOUT_S=15
"""
import logging
import os
import shlex
import sys
from contextlib import AsyncExitStack
from pathlib import Path

# Configure stderr logging BEFORE importing the SDK so nothing lands on stdout.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [gateway] %(message)s")
log = logging.getLogger("gateway")

import anyio
import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from gq_tool_map import map_tool_to_action

# ---- config ------------------------------------------------------------------
LEDGER_URL = os.environ.get("LEDGER_URL", "http://localhost:8000").rstrip("/")
LEDGER_PROJECT_ID = os.environ.get("LEDGER_PROJECT_ID", "great-question")
GATEWAY_AGENT_ID = os.environ.get("GATEWAY_AGENT_ID", "gq-research-agent")

GQ_MCP_URL = os.environ.get("GQ_MCP_URL", "").strip()
GQ_MCP_TOKEN = os.environ.get("GQ_MCP_TOKEN", "").strip()
GQ_MCP_TRANSPORT = os.environ.get("GQ_MCP_TRANSPORT", "http").strip().lower()
GQ_MCP_CMD = os.environ.get("GQ_MCP_CMD", "").strip()
GQ_MCP_ARGS = shlex.split(os.environ.get("GQ_MCP_ARGS", ""))

SANDBOX_ON = os.environ.get("SANDBOX_TOOLS", "on").strip().lower() in ("1", "on", "true", "yes")
SANDBOX_CMD = os.environ.get("SANDBOX_CMD", sys.executable or "python3")
SANDBOX_PATH = str(Path(__file__).resolve().parent / "gq_sandbox_server.py")

APPROVAL_TIMEOUT_S = float(os.environ.get("APPROVAL_TIMEOUT_S", "120"))
APPROVAL_POLL_S = float(os.environ.get("APPROVAL_POLL_S", "1.0"))
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "15"))

# ---- runtime state -----------------------------------------------------------
research_goal = "general customer research"          # updated by set_research_goal
tool_registry: dict[str, ClientSession] = {}         # tool name -> owning session
tool_defs: list[types.Tool] = []                     # merged downstream tool list
http: httpx.AsyncClient = None  # type: ignore

server = Server("ledger-gateway")

SET_GOAL_TOOL = types.Tool(
    name="set_research_goal",
    description=(
        "Record the user's research objective for this session. CALL THIS FIRST, "
        "before any research tools. Ledger uses it to judge whether later actions "
        "actually serve the stated goal (intent-alignment governance)."
    ),
    inputSchema={
        "type": "object",
        "properties": {"goal": {"type": "string", "description": "The research objective in one sentence."}},
        "required": ["goal"],
    },
)


def _err(msg: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=True)


async def submit_to_ledger(action: dict) -> dict:
    payload = {
        "agent_id": GATEWAY_AGENT_ID,
        "task": research_goal,
        "action": action,
        "project_id": LEDGER_PROJECT_ID,
    }
    r = await http.post("/agent/action", json=payload)
    r.raise_for_status()
    return r.json()


async def wait_for_human(action_id: str) -> str:
    """Poll the audit row until a human decides, or we time out.
    Watches human_decision (NOT verdict, which stays 'pending_approval')."""
    deadline = anyio.current_time() + APPROVAL_TIMEOUT_S
    while anyio.current_time() < deadline:
        try:
            r = await http.get(f"/audit/{action_id}")
            if r.status_code == 200:
                decision = r.json().get("human_decision")
                if decision in ("approved", "denied"):
                    return decision
        except Exception:
            log.warning("poll error for %s", action_id, exc_info=True)
        await anyio.sleep(APPROVAL_POLL_S)
    return "timeout"


async def forward(name: str, arguments: dict, note: str) -> types.CallToolResult:
    session = tool_registry[name]
    try:
        result = await session.call_tool(name, arguments)
    except Exception as e:
        log.exception("downstream call failed: %s", name)
        return _err(f"Downstream tool '{name}' failed after approval: {e}")
    content = list(result.content) + [types.TextContent(type="text", text=note)]
    return types.CallToolResult(
        content=content,
        isError=bool(result.isError),
        structuredContent=result.structuredContent,
    )


async def govern_then_call(name: str, arguments: dict) -> types.CallToolResult:
    if name not in tool_registry:
        return _err(f"Unknown tool '{name}'.")
    action = map_tool_to_action(name, arguments)
    try:
        resp = await submit_to_ledger(action)
    except Exception as e:
        log.exception("ledger /agent/action failed")
        # Fail-safe: if the policy layer is unreachable, do not execute.
        return _err(f"Ledger policy check failed ({e}). '{name}' was NOT executed (fail-safe).")

    verdict = resp.get("verdict")
    action_id = resp.get("action_id")
    rule = resp.get("rule_violated")
    reasoning = resp.get("reasoning", "")
    sha = (resp.get("sha256") or "")[:12]

    if verdict == "approved":
        log.info("APPROVED %s (%s) sha=%s", name, action["category"], sha)
        return await forward(name, arguments, f"[Ledger ✓ approved · {action['category']} · sha256 {sha}]")

    # pending_approval — block until a human decides.
    log.info("PENDING %s (%s) rule=%s id=%s — awaiting human", name, action["category"], rule, action_id)
    decision = await wait_for_human(action_id)
    if decision == "approved":
        log.info("HUMAN-APPROVED %s id=%s", name, action_id)
        return await forward(name, arguments, f"[Ledger ✓ human-approved · was flagged: {rule} · sha256 {sha}]")
    if decision == "denied":
        log.info("DENIED %s id=%s", name, action_id)
        return _err(
            f"DENIED by a human reviewer — '{name}' was NOT executed. "
            f"Flagged by: {rule or 'policy'}. {reasoning} (audit id {action_id})"
        )
    log.info("TIMEOUT %s id=%s", name, action_id)
    return _err(
        f"'{name}' is still awaiting human approval after {APPROVAL_TIMEOUT_S:.0f}s and was NOT executed. "
        f"Flagged by: {rule or 'policy'}. Approve it in the Ledger dashboard/Slack, then retry. (audit id {action_id})"
    )


@server.list_tools()
async def list_tools():
    return [SET_GOAL_TOOL] + tool_defs


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    args = arguments or {}
    if name == "set_research_goal":
        global research_goal
        goal = args.get("goal")
        if goal:
            research_goal = goal
            log.info("research goal: %s", research_goal)
        return [types.TextContent(type="text", text=f"Research goal recorded. Ledger will govern actions against: “{research_goal}”")]
    return await govern_then_call(name, args)


async def _register(session: ClientSession, label: str) -> None:
    res = await session.list_tools()
    added = []
    for tool in res.tools:
        if tool.name in tool_registry:
            log.info("skip duplicate tool '%s' from %s (already provided)", tool.name, label)
            continue
        tool_registry[tool.name] = session
        tool_defs.append(tool)
        added.append(tool.name)
    log.info("%s: registered %d tools [%s]", label, len(added), ", ".join(added))


async def _connect_real(stack: AsyncExitStack) -> None:
    headers = {"Authorization": f"Bearer {GQ_MCP_TOKEN}"} if GQ_MCP_TOKEN else None
    try:
        if GQ_MCP_TRANSPORT == "http":
            read, write, _ = await stack.enter_async_context(streamablehttp_client(GQ_MCP_URL, headers=headers))
        elif GQ_MCP_TRANSPORT == "sse":
            read, write = await stack.enter_async_context(sse_client(GQ_MCP_URL, headers=headers))
        elif GQ_MCP_TRANSPORT == "stdio":
            params = StdioServerParameters(command=GQ_MCP_CMD or "npx", args=GQ_MCP_ARGS)
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            log.error("unknown GQ_MCP_TRANSPORT=%s — skipping real server", GQ_MCP_TRANSPORT)
            return
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        await _register(session, f"great-question[{GQ_MCP_TRANSPORT}]")
    except Exception as e:
        # The real server is best-effort. If it's unreachable, the gateway still
        # runs on the sandbox so the demo never hard-fails.
        log.error("could not connect to real GQ server (%s): %s", GQ_MCP_URL, e)


async def _connect_sandbox(stack: AsyncExitStack) -> None:
    params = StdioServerParameters(command=SANDBOX_CMD, args=[SANDBOX_PATH])
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    await _register(session, "sandbox")


async def main() -> None:
    global http
    log.info("Ledger=%s project=%s | GQ=%s (%s) | sandbox=%s",
             LEDGER_URL, LEDGER_PROJECT_ID, GQ_MCP_URL or "—", GQ_MCP_TRANSPORT, "on" if SANDBOX_ON else "off")
    http = httpx.AsyncClient(base_url=LEDGER_URL, timeout=HTTP_TIMEOUT_S)
    try:
        async with AsyncExitStack() as stack:
            if GQ_MCP_URL:
                await _connect_real(stack)
            if SANDBOX_ON:
                await _connect_sandbox(stack)
            if not tool_registry:
                log.warning("no downstream tools registered — only set_research_goal will be available")
            log.info("gateway up: %d governed tools + set_research_goal", len(tool_defs))
            async with stdio_server() as (in_read, in_write):
                await server.run(in_read, in_write, server.create_initialization_options())
    finally:
        await http.aclose()


if __name__ == "__main__":
    anyio.run(main)
