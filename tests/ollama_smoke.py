"""Minimal open-model tool-calling loop: Ollama chat model <-> transport-lit MCP server over stdio.

    ollama pull qwen2.5:3b
    uv run python tests/ollama_smoke.py "Find reports about driver improvement programs; list 3 titles with years."
"""

import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


async def main(question: str) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "transport_lit.server"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = (await s.list_tools()).tools
        oa_tools = [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:600],
                                                      "parameters": getattr(t, "input_schema", None) or getattr(t, "inputSchema", {})}} for t in tools]
        msgs = [{"role": "system", "content": "You are a research assistant. Use the tools to answer; cite ids."},
                {"role": "user", "content": question}]
        calls = 0
        async with httpx.AsyncClient(timeout=300) as http:
            for _ in range(6):
                resp = await http.post(f"{OLLAMA}/api/chat", json={"model": MODEL, "messages": msgs, "tools": oa_tools, "stream": False})
                resp.raise_for_status()
                m = resp.json()["message"]
                msgs.append(m)
                if not m.get("tool_calls"):
                    print("ASSISTANT:", m.get("content"))
                    return 0 if calls else 2
                for tc in m["tool_calls"]:
                    fn = tc["function"]
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        args = json.loads(args)
                    print(f"TOOL CALL: {fn['name']}({json.dumps(args)})")
                    res = await s.call_tool(fn["name"], args)
                    text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                    print(f"  -> {len(text)} chars")
                    msgs.append({"role": "tool", "content": text[:6000], "tool_name": fn["name"]})
                    calls += 1
        print("gave up after 6 rounds")
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]) or "Find reports about driver improvement programs; list 3 titles with years and ids.")))
