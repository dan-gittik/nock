"""The plumbing every module shares: time, atomic files, JSON and slugs."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nock.errors import PreconditionError, UsageError

SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")


def now() -> str:
    """The current moment as an ISO-8601 UTC timestamp, to the second."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def slug(text: str, limit: int = 48) -> str:
    """A lowercase, hyphenated, filesystem-safe name for `text`."""
    return SLUG_CHARACTER.sub("-", text.lower()).strip("-")[:limit].strip("-") or "untitled"


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` so that a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text)
    temporary.replace(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, OSError) as error:
        raise PreconditionError(f"{path} is not readable JSON: {error}") from error


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def payload(argument: str | None, stdin_text: str | None = None) -> Any:
    """A record passed on the command line: inline JSON, a path to a file
    holding it, or — when the argument is omitted — JSON on stdin."""
    if argument is None:
        if stdin_text is None or not stdin_text.strip():
            raise UsageError("expected a JSON argument, a path to a JSON file, or JSON on stdin")
        text = stdin_text
    elif not argument.lstrip().startswith(("{", "[")) and Path(argument).is_file():
        text = Path(argument).read_text()
    else:
        text = argument
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise UsageError(f"not valid JSON: {error}") from error


def format_duration(seconds: float | None) -> str:
    """A duration as a terse `1h 2m` / `3m 4s` / `5s`, or `?` when unknown."""
    if seconds is None:
        return "?"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, rest = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {rest}s"
    return f"{rest}s"
