"""The framework a tool is written against: its signature is its command line,
it asks for fixtures by name, its return value is rendered, its failures are
exit codes — and every call is recorded."""

from __future__ import annotations

import json

from nock.project import Project
from nock.render import capped, render

from .conftest import nock, write_tool

TOOLS = '''\
from dataclasses import dataclass
from enum import StrEnum
from typing import NotRequired, TypedDict

import nock

EVENTS = []


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Person:
    name: str
    age: int = 0


class Filter(TypedDict):
    team: str
    limit: NotRequired[int]


@nock.fixture
def greeting(root):
    EVENTS.append("open")
    yield f"hello from {root.name}"
    print("teardown", flush=True)


@nock.command(group="house")
def hello(name: str, greeting, *, loud: bool = False, times: int = 1, color: Color = Color.RED):
    """Greet someone. In color."""
    line = f"{greeting}, {name} in {color.value}"
    return (line.upper() if loud else line) * times


@nock.command
def person(person: Person) -> Person:
    """Echo a person."""
    return person


@nock.command
def team(filter: Filter) -> list[dict]:
    """A team, filtered."""
    rows = [{"name": f"p{index}", "team": filter["team"], "age": index} for index in range(filter.get("limit", 3))]
    return rows


@nock.command
def nothing() -> None:
    """Say nothing."""


@nock.command
def refuse() -> None:
    """Say no, with evidence."""
    raise nock.Failed("2 problems", ["a is wrong", "b is wrong"])


@nock.command
def predicate(*, answer: bool = False) -> bool:
    """Yes or no."""
    return answer


@nock.command
def passthrough(*, code: int = 3) -> None:
    """Exit with a code."""
    raise nock.Exit(code)


@nock.command
def missing() -> None:
    """Not found."""
    raise nock.NotFoundError("no such thing")


@nock.command
def boom(*, index: int = 5) -> int:
    """Crash."""
    numbers = [1, 2]
    return numbers[index]


@nock.command
def many(*, count: int = 200) -> list[dict]:
    """A long table."""
    return [{"n": index, "square": index * index} for index in range(count)]
'''


def test_the_signature_is_the_command_line(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    name = project.root.name
    result = nock(
        project.root, "house", "hello", "dan", "--loud", "--times", "2", "--color", "blue"
    )
    assert result.returncode == 0, result.stderr
    line = f"HELLO FROM {name.upper()}, DAN IN BLUE"
    assert (
        result.stdout == f"teardown\n{line * 2}\n"
    )  # torn down when the call ends, before printing
    help_text = nock(project.root, "house", "hello", "--help").stdout
    assert "Greet someone" in help_text and "--color" in help_text and "greeting" not in help_text
    bad = nock(project.root, "house", "hello", "dan", "--color", "green")
    assert bad.returncode == 40 and "invalid choice" in bad.stderr


def test_a_record_parameter_takes_json_checked_against_its_type(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    shown = nock(project.root, "person", '{"name": "dan", "age": 3}')
    assert shown.returncode == 0 and shown.stdout == "name: dan\nage: 3\n"
    typo = nock(project.root, "person", '{"name": "dan", "agee": 3}')
    assert typo.returncode == 40 and "did you mean 'age'" in typo.stderr
    from_stdin = nock(project.root, "team", stdin='{"team": "a", "limit": 2}')
    assert from_stdin.stdout == "name,team,age\np0,a,0\np1,a,1\n"
    assert "team: string" in nock(project.root, "team", "--help").stdout


def test_return_values_and_failures_become_output_and_exit_codes(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    assert nock(project.root, "nothing").stdout == ""
    refused = nock(project.root, "refuse")
    assert refused.returncode == 1
    assert refused.stdout == "a is wrong\nb is wrong\n" and "2 problems" in refused.stderr
    assert nock(project.root, "predicate").returncode == 1
    assert nock(project.root, "predicate", "--answer").returncode == 0
    assert nock(project.root, "passthrough", "--code", "7").returncode == 7
    assert nock(project.root, "missing").returncode == 44
    as_json = nock(project.root, "team", '{"team": "b", "limit": 1}', "--json")
    assert json.loads(as_json.stdout) == [{"name": "p0", "team": "b", "age": 0}]


def test_a_crash_shows_the_tool_s_own_line_and_its_values(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    result = nock(project.root, "boom")
    assert result.returncode == 50
    assert ".nock/tools/house.py" in result.stderr and "return numbers[index]" in result.stderr
    assert "numbers = [1, 2]" in result.stderr and "index = 5" in result.stderr
    assert "cli.py" not in result.stderr


def test_long_output_is_capped_and_the_rest_saved(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    result = nock(project.root, "many")
    lines = result.stdout.splitlines()
    assert lines[0] == "n,square" and "… 140 more rows" in result.stdout
    saved = lines[-1].split(": ", 1)[1].split(",")[0]
    assert (project.root / saved).read_text().count("\n") == 201
    assert len(nock(project.root, "many", "--full").stdout.splitlines()) == 201


def test_a_wrong_call_says_what_was_meant_and_is_recorded(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    unknown = nock(project.root, "persn", "{}")
    assert unknown.returncode == 40 and "did you mean `person`" in unknown.stderr
    option = nock(project.root, "house", "hello", "dan", "--lod")
    assert option.returncode == 40 and "did you mean --loud" in option.stderr
    calls = project.store.rows("SELECT tool, exit_code FROM calls ORDER BY id")
    assert calls == [{"tool": "persn", "exit_code": 40}, {"tool": "house hello", "exit_code": 40}]


def test_every_call_is_recorded_with_what_it_cost(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    nock(project.root, "team", '{"team": "a"}')
    nock(project.root, "boom")
    rows = project.store.rows("SELECT * FROM calls ORDER BY id")
    team, boom = rows
    assert team["tool"] == "team" and json.loads(team["arguments"]) == ['{"team": "a"}']
    assert team["exit_code"] == 0 and team["output_bytes"] == len(
        "name,team,age\np0,a,0\np1,a,1\np2,a,2"
    )
    assert team["source"] == ".nock/tools/house.py" and team["duration"] > 0
    assert boom["exit_code"] == 50 and boom["error"] == "IndexError: list index out of range"
    listed = nock(project.root, "calls", "--failed")
    assert "boom" in listed.stdout and "team" not in listed.stdout


def test_the_listing_puts_the_project_s_tools_first(project: Project) -> None:
    write_tool(project, "house", TOOLS)
    listing = nock(project.root).stdout
    assert listing.startswith(
        "Project tools:\n  house hello NAME [--loud] [--times TIMES] [--color COLOR] — Greet someone\n"
    )
    assert "\nnock:\n  check" in listing


def test_a_tool_that_does_not_load_is_reported_on_every_call(project: Project) -> None:
    write_tool(project, "broken", "import nock\n\nraise RuntimeError('oops')\n")
    result = nock(project.root, "calls")
    assert result.returncode == 0
    assert "a tool did not load — .nock/tools/broken.py:3: RuntimeError: oops" in result.stderr
    assert nock(project.root, "check", "nock-tools").returncode == 1


def test_rendering_by_shape() -> None:
    assert render(None) == "" and render(True) == ""
    assert render("text\n\n") == "text"
    assert render({"a": 1, "b": None, "c": True}) == "a: 1\nb: -\nc: yes"
    assert render(["x", 2]) == "x\n2"
    assert render([]) == "(none)"
    rows = [{"name": f"n{index}", "kind": "same", "size": index} for index in range(5)]
    assert render(rows).split("\n")[:2] == ["# kind: same (every row)", "name,size"]
    assert render({"outer": {"inner": [1, {"deep": "x"}]}, "text": "a\nb"}) == (
        "outer:\n  inner:\n    - 1\n    - deep: x\ntext: |\n  a\n  b"
    )
    text, cut = capped("\n".join(["header,x", *(f"{index},y" for index in range(10))]), 8000, 3)
    assert cut and text.endswith("… 7 more rows")
