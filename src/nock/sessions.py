"""Claude Code sessions, read from their transcripts into the telemetry store:
what the human asked, every tool call the agent made — with its input, how
long it took, whether it failed and how much it printed — and what the session
cost. Best effort over an internal format: a record that fits no shape is
skipped, never a crash. Transcripts stay on the machine that wrote them."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.utils import now, parse_moment, read_json

if TYPE_CHECKING:
    from nock.project import Project

INTERRUPTED = "[Request interrupted by user"
PROMPT_LIMIT = 600
INPUT_LIMIT = 400
# The field of each tool's input that says what it was used on.
INPUT_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Agent": "description",
    "Task": "description",
    "Skill": "skill",
}


def configuration_directory() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def encoded(path: Path) -> str:
    """How Claude Code names a project's transcript directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def transcript_directories(root: Path) -> list[Path]:
    """The transcript directories of the checkout at `root` and of the
    worktrees nock puts beside it."""
    projects = configuration_directory() / "projects"
    if not projects.is_dir():
        return []
    prefix = encoded(root)
    return sorted(
        directory
        for directory in projects.iterdir()
        if directory.is_dir()
        and (directory.name == prefix or directory.name.startswith(prefix + "-"))
    )


def find_transcript(root: Path, session: str) -> Path | None:
    for directory in transcript_directories(root):
        candidate = directory / f"{session}.jsonl"
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Event:
    moment: str | None
    tool: str
    input: str
    failed: bool = False
    output_bytes: int = 0
    duration: float | None = None


@dataclass
class Transcript:
    """What one transcript says, reduced to what telemetry keeps."""

    session: str
    directory: str | None = None
    title: str | None = None
    started: str | None = None
    ended: str | None = None
    prompts: list[tuple[str | None, str]] = field(default_factory=list)
    interrupts: int = 0
    compactions: int = 0
    models: Counter[str] = field(default_factory=Counter)
    tokens: Counter[str] = field(default_factory=Counter)
    cost: float | None = None
    events: list[Event] = field(default_factory=list)
    pending: dict[str, Event] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path) -> Transcript:
        transcript = cls(session=path.stem)
        seen: set[str] = set()
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    transcript.fold(record, seen)
        return transcript

    def fold(self, record: dict[str, Any], seen: set[str]) -> None:
        kind = record.get("type")
        moment = record.get("timestamp")
        if isinstance(moment, str) and parse_moment(moment):
            self.started = self.started or moment
            self.ended = moment
        else:
            moment = None
        if kind == "ai-title":
            self.title = record.get("aiTitle") or self.title
        elif kind == "cost-state":
            self.cost = record.get("totalCostUSD", self.cost)
        if record.get("isSidechain") or record.get("parent_tool_use_id"):
            return
        if self.directory is None and isinstance(record.get("cwd"), str):
            self.directory = record["cwd"]
        message = record.get("message")
        if not isinstance(message, dict):
            return
        if kind == "assistant":
            self.fold_assistant(message, moment, seen)
        elif kind == "user":
            self.fold_user(record, message, moment)

    def fold_assistant(self, message: dict[str, Any], moment: str | None, seen: set[str]) -> None:
        identity = str(message.get("id") or "")
        usage = message.get("usage")
        if identity and identity not in seen and isinstance(usage, dict):
            seen.add(identity)
            model = str(message.get("model") or "unknown")
            if model != "<synthetic>":
                self.models[model] += 1
            self.tokens["input"] += int(usage.get("input_tokens") or 0)
            self.tokens["output"] += int(usage.get("output_tokens") or 0)
            self.tokens["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            self.tokens["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            event = Event(moment, name, self.summarize(name, block.get("input")))
            self.events.append(event)
            self.pending[str(block.get("id"))] = event

    def summarize(self, name: str, raw: object) -> str:
        values: dict[str, Any] = raw if isinstance(raw, dict) else {}
        key = INPUT_FIELDS.get(name)
        value = values.get(key) if key else None
        text = str(value) if value is not None else json.dumps(values, ensure_ascii=False)
        if self.directory:
            text = text.replace(f"{self.directory}/", "")
        if name == "Grep" and values.get("path"):
            text += f" in {values['path']}"
        return text[:INPUT_LIMIT]

    def fold_user(
        self, record: dict[str, Any], message: dict[str, Any], moment: str | None
    ) -> None:
        if record.get("isCompactSummary"):
            self.compactions += 1
            return
        content = message.get("content")
        if isinstance(content, str):
            if record.get("isMeta") or content.startswith("<"):
                return
            if content.startswith(INTERRUPTED):
                self.interrupts += 1
                return
            self.prompts.append((moment, content[:PROMPT_LIMIT]))
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                if text.startswith(INTERRUPTED):
                    self.interrupts += 1
                elif not record.get("isMeta") and text and not text.startswith("<"):
                    self.prompts.append((moment, text[:PROMPT_LIMIT]))
            if block.get("type") != "tool_result":
                continue
            event = self.pending.pop(str(block.get("tool_use_id")), None)
            if event is None:
                continue
            event.failed = bool(block.get("is_error"))
            event.output_bytes = len(result_text(block.get("content")).encode())
            start, end = parse_moment(event.moment), parse_moment(moment)
            if start and end:
                event.duration = round((end - start).total_seconds(), 3)


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class Sessions:
    def __init__(self, project: Project) -> None:
        self.project = project

    def note_dispatch(self, session: str, role: str) -> None:
        path = self.project.local / "dispatched" / f"{session}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"role": role}))

    def role(self, session: str) -> str:
        path = self.project.local / "dispatched" / f"{session}.json"
        if path.is_file():
            try:
                return str(read_json(path).get("role") or "interactive")
            except Exception:
                return "interactive"
        return "interactive"

    def ingest(self, path: Path) -> Transcript:
        """Read one transcript into the store, replacing what an earlier read left."""
        transcript = Transcript.read(path)
        session = transcript.session
        tokens = transcript.tokens
        model = transcript.models.most_common(1)[0][0] if transcript.models else None
        title = transcript.title or (transcript.prompts[0][1][:120] if transcript.prompts else None)
        with self.project.store.connection() as connection:
            for table, column in (
                ("sessions", "id"),
                ("prompts", "session"),
                ("events", "session"),
            ):
                connection.execute(f"DELETE FROM {table} WHERE {column} = ?", (session,))
            connection.execute(
                "INSERT INTO sessions (id, role, title, started, ended, model, prompts, interrupts,"
                " compactions, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
                " cost, transcript, ingested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session,
                    self.role(session),
                    title,
                    transcript.started,
                    transcript.ended,
                    model,
                    len(transcript.prompts),
                    transcript.interrupts,
                    transcript.compactions,
                    tokens["input"],
                    tokens["output"],
                    tokens["cache_read"],
                    tokens["cache_write"],
                    transcript.cost if transcript.cost is not None else self.price(model, tokens),
                    str(path),
                    now(),
                ),
            )
            connection.executemany(
                "INSERT INTO prompts (session, moment, text) VALUES (?, ?, ?)",
                [(session, moment, text) for moment, text in transcript.prompts],
            )
            connection.executemany(
                "INSERT INTO events (session, moment, tool, input, failed, output_bytes, duration)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session,
                        event.moment,
                        event.tool,
                        event.input,
                        int(event.failed),
                        event.output_bytes,
                        event.duration,
                    )
                    for event in transcript.events
                ],
            )
        return transcript

    def price(self, model: str | None, tokens: Counter[str]) -> float | None:
        if not model:
            return None
        for prefix, rates in self.project.configuration.section("pricing").items():
            if model.startswith(prefix) and isinstance(rates, dict):
                return sum(tokens[kind] * float(rates.get(kind, 0)) for kind in tokens) / 1_000_000
        return None

    def catch_up(self) -> int:
        """Read every transcript of this project written since it was last read;
        returns how many were."""
        known = {
            row["id"]: row["ingested"]
            for row in self.project.store.rows("SELECT id, ingested FROM sessions")
        }
        count = 0
        for directory in transcript_directories(self.project.root):
            for path in sorted(directory.glob("*.jsonl")):
                ingested = parse_moment(known.get(path.stem))
                if ingested is not None and ingested.timestamp() >= path.stat().st_mtime:
                    continue
                self.ingest(path)
                count += 1
        return count

    def since(self, moment: str | None) -> list[dict[str, Any]]:
        """Sessions that ended after `moment` (all of them when None), oldest first,
        reflection rounds' own sessions left out."""
        rows = self.project.store.rows(
            "SELECT * FROM sessions WHERE role != 'reflector' ORDER BY started"
        )
        start = parse_moment(moment)
        if start is None:
            return rows
        return [row for row in rows if (parse_moment(row["ended"]) or start) > start]
