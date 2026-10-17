"""The dispatcher: a fresh headless agent session launched with a brief, under
supervision, and read into telemetry when it ends."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nock.processes import Supervisor
from nock.project import ROLE_VARIABLE, SESSION_VARIABLE
from nock.sessions import Sessions, find_transcript

if TYPE_CHECKING:
    from nock.project import Project

# The longest brief a command line carries whole; a longer one waits in a file.
ARGUMENT_LIMIT = 100_000
# The variables of the parent Claude Code session a child must not inherit.
PARENT_SESSION_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
)


@dataclass
class Session:
    """A finished dispatched session: its id, its run, how it ended and its
    final message."""

    id: str
    run: int | None
    exit_code: int
    result: str | None
    log: Path


class Dispatcher:
    def __init__(self, project: Project) -> None:
        self.project = project

    def launch(
        self, role: str, brief: str, directory: Path, variables: dict[str, str] | None = None
    ) -> Session:
        session = str(uuid.uuid4())
        settings = self.project.configuration
        command = [*settings.get("reflect.command")]
        command += ["-p", "--output-format", "stream-json", "--verbose", "--session-id", session]
        command += ["--permission-mode", "bypassPermissions"]
        model = str(settings.get("reflect.model") or "")
        if model:
            command += ["--model", model]
        command += ["--", self.carried(session, brief)]
        Sessions(self.project).note_dispatch(session, role)
        output = Supervisor(
            self.project,
            command,
            directory=directory,
            deadline=float(settings.get("reflect.deadline")),
            silence=float(settings.get("reflect.silence")),
            environment={**self.environment(session, role), **(variables or {})},
            session=session,
        ).run()
        transcript = find_transcript(self.project.root, session)
        if transcript is not None:
            Sessions(self.project).ingest(transcript)
        return Session(session, output.run, output.code, final_result(output.raw), output.log)

    def environment(self, session: str, role: str) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if key not in PARENT_SESSION_VARIABLES
        }
        environment[ROLE_VARIABLE] = role
        environment[SESSION_VARIABLE] = session
        return environment

    def carried(self, session: str, brief: str) -> str:
        if len(brief.encode()) <= ARGUMENT_LIMIT:
            return brief
        path = self.project.local / "briefs" / f"{session}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief)
        return f"Your brief is {len(brief.encode())} bytes, too long for a command line: read {path} in full before anything else."


def final_result(stream: str) -> str | None:
    """The session's last word: the `result` line of a stream-json transcript."""
    for line in reversed(stream.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            result = record.get("result")
            return result if isinstance(result, str) else None
    return None
