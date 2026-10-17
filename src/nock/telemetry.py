"""Telemetry of the toolbox: every call recorded — what was asked, how long it
took, how it ended, how much the caller had to read — a note when a failing
call repeats one before it to the byte, and the cache that replays a result
whose inputs have not changed."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.git import Git, GitError
from nock.utils import format_duration, now, parse_moment

if TYPE_CHECKING:
    from nock.project import Project
    from nock.tools import Tool

# Set on calls that stay out of telemetry: `nock check` running tools' examples.
UNRECORDED_VARIABLE = "NOCK_UNRECORDED"
# How far back a repeated failure is looked for, in seconds.
REPEAT_WINDOW = 6 * 3600


def digest(*parts: str | bytes) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part if isinstance(part, bytes) else part.encode())
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def tree_state(root: Path) -> str | None:
    """A fingerprint of the working tree: HEAD, and each uncommitted file's
    size and modification time."""
    git = Git(root)
    try:
        head = git("rev-parse", "HEAD", check=False)
        dirty = git.dirty()
    except GitError:
        return None
    parts = [head]
    for path in dirty:
        try:
            stat = (root / path).stat()
            parts.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            parts.append(f"{path}:gone")
    return digest(*parts)


class Calls:
    def __init__(self, project: Project) -> None:
        self.project = project

    def start(self, tool: str, arguments: list[str], source: str | None) -> int:
        return self.project.store.insert(
            "calls",
            {
                "tool": tool,
                "arguments": json.dumps(arguments),
                "source": source,
                "session": self.project.current_session(),
                "directory": str(self.project.here),
                "started": now(),
            },
        )

    def finish(self, call: int, values: dict[str, Any]) -> None:
        self.project.store.update("calls", call, values)

    def repeat(
        self, call: int, tool: str, arguments: list[str], fingerprint: str, tree: str | None
    ) -> str | None:
        """A note when this failing call printed exactly what an earlier call
        with the same arguments printed — and whether anything changed between."""
        rows = self.project.store.rows(
            "SELECT id, started, tree FROM calls WHERE tool = ? AND arguments = ? AND fingerprint = ?"
            " AND id < ? AND exit_code != 0 ORDER BY id DESC LIMIT 1",
            (tool, json.dumps(arguments), fingerprint, call),
        )
        if not rows:
            return None
        earlier = rows[0]
        moment = parse_moment(earlier["started"])
        age = time.time() - moment.timestamp() if moment else None
        if age is None or age > REPEAT_WINDOW:
            return None
        same_tree = tree is not None and tree == earlier["tree"]
        tail = " and no file has changed since" if same_tree else ""
        return f"same result as call {earlier['id']} ({format_duration(age)} ago){tail}"


class Cache:
    """Results replayed while their inputs stand still: the key is the tool's
    code, its arguments and the state of the files its globs match."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.directory = project.local / "cache"

    def key(self, tool: Tool, arguments: list[str]) -> str:
        parts: list[str | bytes] = [tool.command.path, json.dumps(arguments)]
        source = self.project.here / tool.source
        if source.is_file():
            parts.append(source.read_bytes())
        root = self.project.here
        for pattern in tool.cache:
            pattern = f"{pattern}/*" if pattern.endswith("**") else pattern
            for path in sorted(root.glob(pattern)):
                if path.is_file() and ".git" not in path.parts:
                    stat = path.stat()
                    parts.append(f"{path.relative_to(root)}:{stat.st_size}:{stat.st_mtime_ns}")
        return digest(*parts)

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, record: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{key}.json").write_text(json.dumps(record))
