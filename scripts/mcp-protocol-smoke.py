#!/usr/bin/env python3
"""Authenticated protocol smoke using the official Python MCP client."""

from __future__ import annotations

import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    url = os.environ["MCP_URL"]
    token = os.environ["MCP_ACCESS_TOKEN"]
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    if initialized.serverInfo.name != "SignalHub MCP" or len(names) != 10:
        raise RuntimeError("Unexpected MCP initialize/tools response")
    print(
        f"sdk_protocol={initialized.protocolVersion} "
        f"sdk_server={initialized.serverInfo.name} sdk_tools={len(names)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
