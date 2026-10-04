"""Seat headless do Band: Claude Code dirigido pelo ClaudeSDKAdapter do band-sdk.

Permissões escopadas (sem bypassPermissions): modo acceptEdits + allowlist de
ferramentas, cwd preso ao repo de resultado, timeout por turno, sem settings do host.

Env: SEAT_NAME, AGENT_ID, AGENT_KEY, MANDATE_FILE, SEAT_CWD, SEAT_MODEL,
     GIT_NAME, GIT_EMAIL, SEAT_TURN_TIMEOUT
"""
import asyncio
import logging
import os
import pathlib
import signal

from band import Agent, Emit
from band.adapters import ClaudeSDKAdapter
from band.adapters.claude_sdk import (
    ClaudeCLIOptions,
    ClaudePermissionMode,
    ClaudeSDKAdapterConfig,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

name = os.environ["SEAT_NAME"]
mandate = pathlib.Path(os.environ["MANDATE_FILE"]).read_text()
cwd = os.environ["SEAT_CWD"]
model = os.environ.get("SEAT_MODEL", "claude-sonnet-5-5")

env = {
    "GIT_AUTHOR_NAME": os.environ.get("GIT_NAME", name),
    "GIT_AUTHOR_EMAIL": os.environ.get("GIT_EMAIL", f"{name}@factory.test"),
    "GIT_COMMITTER_NAME": os.environ.get("GIT_NAME", name),
    "GIT_COMMITTER_EMAIL": os.environ.get("GIT_EMAIL", f"{name}@factory.test"),
    "PATH": os.environ["PATH"],
    "HOME": os.environ["HOME"],
}

ALLOWED = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,LS,TodoWrite"
DISALLOWED = "WebFetch,WebSearch"

cfg = ClaudeSDKAdapterConfig(
    model=model,
    custom_section=mandate,
    permission_mode=ClaudePermissionMode.AUTO,
    cwd=cwd,
    setting_sources=(),
    turn_timeout_s=float(os.environ.get("SEAT_TURN_TIMEOUT", "5400")),
    cli=ClaudeCLIOptions(
        cli_path=os.path.expanduser("~/.local/bin/claude"),
        env=env,
    ),
)
adapter = ClaudeSDKAdapter(cfg, emit={Emit.TOOL_CALLS, Emit.USAGE})
agent = Agent.create(adapter=adapter, agent_id=os.environ["AGENT_ID"], api_key=os.environ["AGENT_KEY"])


async def main() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    task = asyncio.create_task(agent.run())
    await asyncio.wait({task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if not task.done():
        await agent.stop()


asyncio.run(main())
