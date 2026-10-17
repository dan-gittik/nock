"""The telemetry store: one SQLite database in `.nock/local/`, never committed —
every call to a nock command, every supervised process, and what each Claude
Code session did, read from its transcript. Reflection reads it; so can any
agent, with `nock query`."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nock.errors import UsageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,          -- the command as typed: `failures`, `gen add`; unknown ones too
    arguments TEXT NOT NULL,     -- JSON list of the words after the command
    source TEXT,                 -- `nock`, or the tools file that defines it
    session TEXT,
    directory TEXT,
    started TEXT NOT NULL,
    duration REAL,               -- seconds
    exit_code INTEGER,
    output_bytes INTEGER,        -- what the caller read, after condensing and capping
    output_lines INTEGER,
    full_bytes INTEGER,          -- what it would have read uncapped
    cached INTEGER DEFAULT 0,
    error TEXT,                  -- the first line of what went wrong
    fingerprint TEXT,            -- hash of the output, to notice a repeat
    tree TEXT                    -- hash of HEAD and the dirty files, on failure
);
CREATE INDEX IF NOT EXISTS calls_tool ON calls (tool, started);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call INTEGER,                -- the nock call that started it
    command TEXT NOT NULL,       -- JSON argv
    directory TEXT,
    session TEXT,
    started TEXT NOT NULL,
    finished TEXT,
    exit_code INTEGER,
    pid INTEGER,
    supervisor INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    role TEXT,                   -- `interactive`, or the role nock dispatched it as
    title TEXT,
    started TEXT,
    ended TEXT,
    model TEXT,
    prompts INTEGER,
    interrupts INTEGER,
    compactions INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost REAL,
    transcript TEXT,
    ingested TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
    session TEXT NOT NULL,
    moment TEXT,
    text TEXT NOT NULL           -- what the human typed, up to 600 characters
);
CREATE INDEX IF NOT EXISTS prompts_session ON prompts (session);
CREATE TABLE IF NOT EXISTS events (
    session TEXT NOT NULL,
    moment TEXT,
    tool TEXT NOT NULL,          -- Claude Code's tool: Bash, Read, Edit, Grep…
    input TEXT,                  -- the command, path or pattern, up to 400 characters
    failed INTEGER,
    output_bytes INTEGER,
    duration REAL                -- seconds between the call and its result
);
CREATE INDEX IF NOT EXISTS events_session ON events (session);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(SCHEMA)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def insert(self, table: str, values: dict[str, Any]) -> int:
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.connection() as connection:
            cursor = connection.execute(
                f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values())
            )
            return int(cursor.lastrowid or 0)

    def update(self, table: str, key: int, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), key)
            )

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(sql, parameters)]

    def query(self, sql: str) -> list[dict[str, Any]]:
        """A read-only query, as `nock query` runs it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection():
            pass  # make sure the schema exists before opening read-only
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql)]
        except sqlite3.Error as error:
            raise UsageError(f"{error} — the tables are: {', '.join(self.tables())}") from error
        finally:
            connection.close()

    def tables(self) -> list[str]:
        return [
            row["name"]
            for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row["name"].startswith("sqlite_")
        ]
