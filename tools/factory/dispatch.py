"""Envia o dispatch humano pro coordinator na room (1 mensagem, menciona só o coordinator).
Uso: dispatch.py <room_id> <arquivo-com-o-texto-do-dispatch>
Lê BAND_USER_KEY/BAND_BASE_URL de ~/.config/band/env (exportar antes: set -a; . ~/.config/band/env; set +a)
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(room: str, path: str) -> None:
    content = open(path, encoding="utf-8").read()
    venv = os.path.dirname(os.path.dirname(sys.executable))
    p = StdioServerParameters(command=os.path.join(venv, "bin", "band-mcp"), args=["--scope", "human"], env=dict(os.environ))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "band_send_my_chat_message",
                {"chat_id": room, "content": content, "recipients": "coordinator"},
            )
            for c in res.content:
                print(c.text[:500])


asyncio.run(main(sys.argv[1], sys.argv[2]))
