"""What a call prints: its return value, rendered as compactly as its shape
allows — nothing for None, a line per item, `key: value` for a record, a header
and CSV rows for a list of records — and a crash, as the few lines of the
tool's own code that matter. Measured, cheapest-for-the-accuracy formats: CSV
for flat tables, `key: value` for single records, indented `key: value` for
anything nested."""

from __future__ import annotations

import ast
import csv
import dataclasses
import io
import json
import linecache
import reprlib
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any

from nock.condense import excerpt

# nock's own code, left out of a crash report when the project's own frames are there.
PACKAGE_INTERNAL = Path(__file__).resolve().parent
SCALARS = (str, int, float, bool, type(None))
# A column with one value in every row is said once, above the table, from this many rows.
HOISTED_FROM = 5
NONE = "(none)"
SHORT = reprlib.Repr(maxstring=120, maxother=120, maxlist=8, maxdict=8)


@dataclasses.dataclass(frozen=True)
class Problem:
    """A check's finding, where it is: printed `path:line: message`."""

    message: str
    path: str | None = None
    line: int | None = None

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.path and self.line else self.path
        return f"{where}: {self.message}" if where else self.message


def plain(value: Any) -> Any:
    """`value` as JSON-shaped data: records, lists and scalars."""
    if isinstance(value, Problem):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: plain(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, Enum):
        return plain(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, SCALARS):
        return value
    if isinstance(value, Iterable):
        return [plain(item) for item in value]
    return str(value)


def as_json(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False, separators=(",", ":"))


def render(value: Any) -> str:
    """The whole rendering of a return value; empty when there is nothing to say."""
    data = plain(value)
    if data is None or isinstance(data, bool):
        return ""
    if isinstance(data, str):
        return data.rstrip()
    if isinstance(data, (int, float)):
        return str(data)
    if isinstance(data, dict):
        return "\n".join(nested(data)) if data else NONE
    if not data:
        return NONE
    if all(isinstance(item, SCALARS) for item in data):
        return "\n".join(scalar(item) for item in data)
    if all(isinstance(item, dict) for item in data) and all(
        isinstance(cell, SCALARS) for item in data for cell in item.values()
    ):
        return table(data)
    return "\n".join(nested(data))


def scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def table(rows: list[dict[str, Any]]) -> str:
    """A header and CSV rows; a column that holds one value throughout is said
    once above them instead."""
    columns: list[str] = []
    for row in rows:
        columns += [key for key in row if key not in columns]
    hoisted = []
    if len(rows) >= HOISTED_FROM:
        for column in list(columns):
            values = {scalar(row.get(column)) for row in rows}
            if len(values) == 1 and len(columns) > 1:
                hoisted.append(f"# {column}: {values.pop()} (every row)")
                columns.remove(column)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([scalar(row.get(column)) for column in columns])
    return "\n".join([*hoisted, buffer.getvalue().rstrip("\n")])


def nested(data: Any, indent: str = "") -> list[str]:
    """Indented `key: value` — YAML's shape, without its quoting rules."""
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{indent}{key}:")
                lines += nested(value, indent + "  ")
            elif isinstance(value, str) and "\n" in value:
                lines.append(f"{indent}{key}: |")
                lines += [f"{indent}  {line}" for line in value.rstrip("\n").split("\n")]
            else:
                lines.append(f"{indent}{key}: {empty(value)}")
        return lines
    for item in data:
        if isinstance(item, (dict, list)) and item:
            inner = nested(item, indent + "  ")
            lines.append(f"{indent}- {inner[0].lstrip()}")
            lines += inner[1:]
        else:
            lines.append(f"{indent}- {empty(item)}")
    return lines


def empty(value: Any) -> str:
    if value == [] or value == {}:
        return NONE
    return scalar(value)


def capped(text: str, characters: int, rows: int) -> tuple[str, bool]:
    """`text` within `characters` and, for a table, `rows` rows; whether
    anything was left out."""
    lines = text.split("\n")
    data = [line for line in lines if not line.startswith("# ")]
    if len(data) > rows + 1 and "," in data[0]:
        shown = lines[: len(lines) - len(data) + rows + 1]
        more = len(data) - rows - 1
        return "\n".join([*shown, f"… {more} more rows"]), True
    return excerpt(lines, characters)


def crash(error: BaseException, internal: Path) -> str:
    """A crash as a reader needs it: what was raised, and each line of the
    project's own code it went through, with the values that line used. nock's
    own frames are left out unless there is nothing else."""
    frames = list(walk(error.__traceback__))
    own = [
        frame for frame in frames if not Path(frame[0].f_code.co_filename).is_relative_to(internal)
    ]
    lines = [f"{type(error).__name__}: {error}"]
    for frame, number in (own or frames[-3:])[-6:]:
        filename = frame.f_code.co_filename
        code = linecache.getline(filename, number).strip()
        lines.append(f"  {shorten(filename)}:{number} in {frame.f_code.co_name}: {code}")
        for name in names_in(code):
            if name in frame.f_locals:
                lines.append(f"      {name} = {SHORT.repr(frame.f_locals[name])}")
    return "\n".join(lines)


def walk(traceback: TracebackType | None) -> Iterable[tuple[Any, int]]:
    while traceback is not None:
        yield traceback.tb_frame, traceback.tb_lineno
        traceback = traceback.tb_next


def names_in(code: str) -> list[str]:
    for candidate in (code, f"{code} pass"):  # a block's opening line parses with a body
        try:
            tree = ast.parse(candidate)
            break
        except SyntaxError:
            continue
    else:
        return []
    seen: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.append(node.id)
    return seen[:5]


def shorten(filename: str) -> str:
    try:
        return str(Path(filename).relative_to(Path.cwd()))
    except ValueError:
        return filename
