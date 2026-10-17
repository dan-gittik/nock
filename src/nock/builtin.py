"""nock's own commands and checks, registered through the same decorators a
project's tools use — they are tools too."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, NotRequired, TypedDict

from nock.checks import Checks
from nock.commands import Option
from nock.errors import Error, ExitCode, Failed, UsageError
from nock.fixtures import Shell
from nock.generators import Catalogs, Generators
from nock.processes import Output
from nock.project import Project
from nock.reflect import ROUND_VARIABLE, Record, Reflection, Rounds, stamp, tool_rows
from nock.schema import check_schema
from nock.sessions import Sessions
from nock.setup import Installer, Upgrader, wired_events
from nock.telemetry import UNRECORDED_VARIABLE
from nock.templates import SYNTAX, Library
from nock.tools import NATIVE_EVENTS, check, command

BRIEFS = Path(__file__).resolve().parent / "briefs"
EXAMPLE_DEADLINE = 300


# --- working -------------------------------------------------------------------


@command(name="check")
def check_command(
    project: Project,
    names: list[str] | None = None,
    *,
    fix: Annotated[bool, Option(help="Run each command check's `fix` first.")] = False,
) -> None:
    """Run the checks — all of them, or those named; prints nothing when all pass.
    Exit 1: the code is wrong. 42 (or 54/55): the machine is."""
    verdict = Checks(project).run(names or None, fix=fix)
    if verdict.exit_code == ExitCode.SUCCESS:
        return
    problems = verdict.problems()
    if verdict.exit_code == ExitCode.SUBJECT_FAILED:
        raise Failed(f"{len(problems)} problem(s)", problems)
    raise Error.for_exit_code(
        verdict.exit_code, "a check could not run — the machine, not the code", problems
    )


@command(name="run", rest="command")
def run_command(
    sh: Shell,
    command: list[str],
    *,
    deadline: Annotated[float | None, Option(help="Kill it after this many seconds.")] = None,
    silence: Annotated[
        float | None, Option(help="Kill it after this many seconds without output.")
    ] = None,
) -> Output:
    """Run any command supervised: bounded, logged, its output condensed (no
    colors or progress bars, repeated lines folded, long output cut to the
    end with the log's path). Exits with the command's code. For anything
    noisy or long: `.nock/nock run -- pytest -x`."""
    if not command:
        raise UsageError("nothing to run: `.nock/nock run -- COMMAND …`")
    return sh(command, check=False, deadline=deadline, silence=silence)


@command
def guide() -> str:
    """How to write a tool, a check, a hook or a generator."""
    return Library(BRIEFS).render("_framework.md").rstrip()


# --- telemetry -------------------------------------------------------------------


@command
def calls(
    project: Project,
    *,
    tool: Annotated[str | None, Option(help="Only this command's calls.")] = None,
    failed: Annotated[bool, Option(help="Only calls that did not exit 0.")] = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Recent calls to nock, newest first: how each was called and how it ended."""
    query = (
        "SELECT id, started, tool, arguments, exit_code, duration, output_bytes, error FROM calls"
    )
    conditions, parameters = [], []
    if tool:
        conditions.append("tool = ?")
        parameters.append(tool)
    if failed:
        conditions.append("exit_code != 0")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = project.store.rows(query + " ORDER BY id DESC LIMIT ?", (*parameters, limit))
    return [
        {
            "id": row["id"],
            "started": (row["started"] or "")[:16].replace("T", " "),
            "call": " ".join([row["tool"], *map(shlex.quote, json_list(row["arguments"]))]),
            "exit": row["exit_code"],
            "seconds": row["duration"],
            "printed": row["output_bytes"],
            "error": row["error"],
        }
        for row in rows
    ]


def json_list(text: str | None) -> list[str]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


@command
def stats(
    project: Project,
    *,
    since: Annotated[
        str | None, Option(help="An ISO date; the default is the last reflection round.")
    ] = None,
) -> list[dict[str, Any]]:
    """Each command's use since the last reflection round: calls, failures,
    duration, how much it printed."""
    window = since if since is not None else Reflection(project).window().since
    keys = (
        "tool",
        "calls",
        "sessions",
        "said_no",
        "usage_errors",
        "machine_errors",
        "median_seconds",
        "mean_bytes",
        "capped",
        "cached",
    )
    return [
        {key: row[key] for key in keys}
        for row in tool_rows(project, window)
        if row["calls"] or row["source"] != "nock"
    ]


@command
def query(project: Project, sql: str) -> list[dict[str, Any]]:
    """Read the telemetry with SQL (read-only). Tables: calls, runs, sessions,
    prompts, events — `.nock/nock reflect material` explains them."""
    return project.store.query(sql)


@command
def sessions(project: Project, *, limit: int = 15) -> list[dict[str, Any]]:
    """Recent Claude Code sessions in this project: what each was, what it cost."""
    Sessions(project).catch_up()
    rows = project.store.rows("SELECT * FROM sessions ORDER BY started DESC LIMIT ?", (limit,))
    return [
        {
            "started": (row["started"] or "")[:16].replace("T", " "),
            "id": row["id"][:8],
            "role": row["role"],
            "title": (row["title"] or "")[:70],
            "prompts": row["prompts"],
            "tokens": sum(
                row[key] or 0
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            ),
            "cost": round(row["cost"], 2) if row["cost"] else None,
        }
        for row in rows
    ]


# --- reflection ---------------------------------------------------------------------


@command(name="run", group="reflect")
def reflect_run(project: Project) -> dict[str, Any]:
    """Run a reflection round: an agent reads the telemetry and the work, and
    changes the toolbox on a branch that lands when the checks pass. Takes a
    while — run it in the background."""
    outcome = Reflection(project).run()
    if outcome.get("landed") is None:
        raise Failed(f"round {outcome['round']} did not land", outcome)
    return outcome


@command(name="due", group="reflect")
def reflect_due(project: Project) -> str | bool:
    """Whether a reflection round is due; exit 1 when it is not."""
    return Reflection(project).due() or False


@command(name="material", group="reflect")
def reflect_material(project: Project) -> str:
    """What the next round would read."""
    return Reflection(project).material()


@command(name="record", group="reflect")
def reflect_record(project: Project, record: Record) -> str:
    """Record the round being run: what changed, why, and what each change
    should do by the next round. Run by the reflecting session."""
    name = os.environ.get(ROUND_VARIABLE) or stamp()
    path = Rounds(project).write(name, record)
    return f"recorded {path.relative_to(project.here)}"


# --- generators -----------------------------------------------------------------------


class Binding(TypedDict):
    template: str
    """A file under the catalog's templates/."""
    file: str
    """The output, relative to the repository."""
    tag: NotRequired[str]
    """Render only the entries whose `tags` include it."""


@command(name="write", group="gen")
def gen_write(project: Project) -> list[str]:
    """Write every generated file that has fallen out of step."""
    return [str(path.relative_to(project.here)) for path in Generators(project).write()]


@command(name="new", group="gen", epilog=SYNTAX)
def gen_new(project: Project, name: str, schema: dict[str, Any]) -> str:
    """Start a catalog: a list the code is generated from (error codes, flags,
    strings). SCHEMA is the JSON schema of one entry; then put templates in
    `.nock/generators/NAME/templates/` and bind each to the file it renders."""
    check_schema(schema)
    catalog = Catalogs(project).new(name, schema)
    return f"created {catalog.directory.relative_to(project.here)}"


@command(name="add", group="gen")
def gen_add(project: Project, name: str, entry: dict[str, Any]) -> list[str]:
    """Add an entry (with its `key`) to a catalog; re-renders its files."""
    return relative(project, Catalogs(project).get(name).add(entry))


@command(name="set", group="gen")
def gen_set(project: Project, name: str, key: str, change: dict[str, Any]) -> list[str]:
    """Change an entry's fields (null removes one); re-renders its files."""
    return relative(project, Catalogs(project).get(name).set(key, change))


@command(name="rm", group="gen")
def gen_remove(project: Project, name: str, key: str) -> list[str]:
    """Remove an entry; re-renders its files."""
    return relative(project, Catalogs(project).get(name).remove(key))


@command(name="show", group="gen")
def gen_show(project: Project, name: str | None = None) -> Any:
    """Every generator — or one catalog: its schema, entries and outputs."""
    if name is not None:
        return Catalogs(project).get(name).summary()
    found: list[dict[str, Any]] = [
        {
            "generator": catalog.name,
            "kind": "catalog",
            "entries": len(catalog.entries()),
            "files": " ".join(binding.file for binding in catalog.bindings()),
        }
        for catalog in Catalogs(project).all()
    ]
    found += [
        {"generator": entry.name, "kind": "function", "entries": None, "files": entry.path}
        for entry in project.registry.generators
    ]
    return found


@command(name="bind", group="gen")
def gen_bind(project: Project, name: str, binding: Binding) -> list[str]:
    """Bind one of a catalog's templates to the file it renders."""
    return relative(project, Catalogs(project).get(name).bind(dict(binding)))


@command(name="unbind", group="gen")
def gen_unbind(project: Project, name: str, file: str) -> list[str]:
    """Stop rendering a file from a catalog (the file stays)."""
    return relative(project, Catalogs(project).get(name).unbind(file))


def relative(project: Project, paths: list[Path]) -> list[str]:
    return [
        str(path.relative_to(project.here)) if path.is_relative_to(project.here) else str(path)
        for path in paths
    ]


# --- setup ------------------------------------------------------------------------------


@command
def init(
    *, source: Annotated[str | None, Option(help="Where upgrades come from.")] = None
) -> list[str]:
    """Install nock into this repository — vendored into `.nock/`, wired into
    Claude Code — or re-wire it. Safe to run again."""
    report = Installer(Path.cwd()).install(source)
    return report.lines()


@command
def upgrade(
    project: Project,
    *,
    source: Annotated[str | None, Option(help="A git URL or a local checkout.")] = None,
    force: Annotated[bool, Option(help="Replace the core, losing local edits to it.")] = False,
) -> list[str]:
    """Upgrade the vendored core, keeping local edits to it (a three-way merge)."""
    return Upgrader(project).upgrade(source, force).lines()


# --- nock's own checks ---------------------------------------------------------------------


@check(name="nock-tools")
def tools_load_and_examples_run(project: Project, root: Path) -> list[str]:
    """Every tool loads, and every example a tool gives runs without being
    called wrong or breaking."""
    problems = [f"does not load — {failure}" for failure in project.registry.failures]
    environment = {
        **os.environ,
        UNRECORDED_VARIABLE: "1",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    for tool in project.registry.project_tools():
        try:
            tool.command.usage()
        except Exception as error:
            problems.append(
                f"{tool.command.path} ({tool.source}): its signature does not make a command: {error}"
            )
            continue
        for example in tool.command.examples:
            arguments = [*tool.command.path.split(), *shlex.split(example)]
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "nock", *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=EXAMPLE_DEADLINE,
                )
            except subprocess.TimeoutExpired:
                problems.append(
                    f"`{' '.join(arguments)}` ({tool.source}) ran past {EXAMPLE_DEADLINE}s"
                )
                continue
            if result.returncode >= 40:
                problems.append(
                    f"`{' '.join(arguments)}` ({tool.source}) exited {result.returncode}: {result.stderr.strip()[-600:]}"
                )
    return problems


@check(name="nock-generators")
def generated_files_in_step(project: Project, root: Path) -> list[str]:
    """Every catalog's entries fit its schema, and no generated file has drifted."""
    problems = []
    for catalog in Catalogs(project).all():
        problems += catalog.problems()
    problems += [
        f"{path} has drifted from its generator — `.nock/nock gen write`"
        for path in Generators(project).drifted(root)
    ]
    return problems


@check(name="nock-wiring")
def hooks_wired(project: Project) -> list[str]:
    """Every event a hook listens to is wired into Claude Code."""
    wired = wired_events(project.here)
    return [
        f"a hook listens to {entry.event} ({entry.source}) but Claude Code does not send it — `.nock/nock init` wires it"
        for entry in project.registry.hooks
        if NATIVE_EVENTS[entry.event] not in wired
    ]
