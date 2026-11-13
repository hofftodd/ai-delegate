"""Start `ai-delegate serve` over stdio like Claude Code does, list its tools and call models."""

import asyncio
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command="uv", args=["run", "ai-delegate", "serve"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        init = await s.initialize()
        print("server:", init.server_info.name, "| instructions:", bool(init.instructions))
        tools = await s.list_tools()
        for t in tools.tools:
            print(f"tool {t.name}: {sorted(t.input_schema.get('properties') or {})}")
        r = await s.call_tool("models", {})
        print(r.content[0].text.splitlines()[2])
        if len(sys.argv) > 1:
            r = await s.call_tool("ask", {"prompt": sys.argv[1]})
            print(r.content[0].text)


asyncio.run(main())
