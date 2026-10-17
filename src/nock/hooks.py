"""Claude Code's hook events, handled: `nock hook EVENT` reads the payload on
stdin and answers in Claude Code's hook format. nock's own: a session starts
knowing the toolbox (and whether a reflection round is due), and its
transcript is read into telemetry when it ends. The project's `@nock.hook`s
run after. Every handler fails open — a nock that cannot load must never block
the tool call that would fix it."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nock.fixtures import Call, Scope
from nock.project import Project
from nock.reflect import Reflection
from nock.sessions import Sessions
from nock.tools import NATIVE_EVENTS, Event

EVENTS = {native: event for event, native in NATIVE_EVENTS.items()}
# The events nock itself always listens to.
BUILT_IN_EVENTS = (Event.SESSION_START, Event.SESSION_END)


class Block(Exception):
    """Raised by a `stop` hook to refuse the end of a turn, with the reason."""


class HookRunner:
    def __init__(self, project: Project, payload: dict[str, Any]) -> None:
        self.project = project
        self.payload = payload

    def handle(self, native: str) -> dict[str, Any] | None:
        event = EVENTS[native]
        messages: list[str] = []
        if event is Event.SESSION_START:
            messages.append(self.session_start())
        elif event is Event.SESSION_END:
            self.session_end()
        blocks: list[str] = []
        for entry in self.project.registry.hooks_for(event):
            values = {
                "project": self.project,
                "payload": self.payload,
                "call": Call(None, f"hook {entry.name}"),
            }
            try:
                with Scope(self.project.registry, values) as scope:
                    result = scope.call(entry.function)
            except Block as block:
                blocks.append(str(block))
                continue
            except Exception as error:
                print(
                    f"nock: hook {entry.name} ({entry.source}) failed: {error!r}", file=sys.stderr
                )
                continue
            if isinstance(result, str) and result.strip():
                messages.append(result.strip())
        if blocks and event is Event.STOP and self.payload.get("stop_hook_active") is not True:
            return {"decision": "block", "reason": "\n".join(blocks)}
        text = "\n\n".join(message for message in messages if message)
        if not text or event in (Event.STOP, Event.SESSION_END):
            return None
        return {"hookSpecificOutput": {"hookEventName": native, "additionalContext": text}}

    def session_start(self) -> str:
        """The toolbox, so the session reaches for a tool before scripting one,
        and — for a session the human started — whether a round is due."""
        registry = self.project.registry
        lines = []
        own = registry.project_tools()
        if own:
            lines.append("This project's nock tools (`.nock/nock NAME --help` for usage):")
            lines += [f"- {tool.command.usage()}: {tool.command.summary}" for tool in own]
        if registry.failures:
            lines += [f"nock: a tool did not load — {failure}" for failure in registry.failures]
        if self.project.current_role() is None:
            due = Reflection(self.project).due()
            if due:
                lines.append(f"nock: {due} — run `.nock/nock reflect run` in the background.")
        return "\n".join(lines)

    def session_end(self) -> None:
        transcript = self.payload.get("transcript_path")
        if isinstance(transcript, str) and Path(transcript).is_file():
            Sessions(self.project).ingest(Path(transcript))


def run(native: str, stdin_text: str) -> int:
    """The hook entry point; always exits 0 — a block is said in the output."""
    if native not in EVENTS:
        print(f"nock: unknown hook event {native!r}; known: {', '.join(EVENTS)}", file=sys.stderr)
        return 0
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        payload = payload if isinstance(payload, dict) else {}
        project = Project.discover(Path(payload.get("cwd") or "."))
        output = HookRunner(project, payload).handle(native)
    except Exception as error:  # fail open: nock must never wedge the session
        print(f"nock: the {native} hook failed and was skipped: {error!r}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output))
    return 0
