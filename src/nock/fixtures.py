"""Fixtures: what a tool gets by asking for it by name, as pytest does — nock's
own, defined here with the same decorator a project uses, and the scope that
resolves them for one call and cleans up after it.

    @nock.command
    def failures(sh, root, *, path: str = "tests") -> list[dict]: ...
"""

from __future__ import annotations

import contextlib
import inspect
import os
import shlex
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.configuration import Configuration
from nock.errors import (
    ExitCode,
    HungError,
    PreconditionError,
    SubjectFailedError,
    TimedOutError,
)
from nock.git import Git
from nock.processes import NOT_RUNNABLE, Output, Supervisor
from nock.tools import fixture
from nock.utils import now

if TYPE_CHECKING:
    from nock.project import Project
    from nock.tools import Registry

# What every process `sh` starts is told, so it prints for a reader, not a terminal.
QUIET_ENVIRONMENT = {"NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"}


@dataclass
class Call:
    """The call being served: its number in the telemetry store, if recorded."""

    id: int | None
    tool: str


class Scope:
    """The fixtures of one call: each made once, on first request, and torn
    down — the code after a fixture's `yield` — when the call ends."""

    def __init__(self, registry: Registry, values: dict[str, Any]) -> None:
        self.registry = registry
        self.values = dict(values)
        self.teardowns: list[Iterator[Any]] = []
        self.resolving: list[str] = []

    def __enter__(self) -> Scope:
        return self

    def __exit__(self, *exception: object) -> None:
        while self.teardowns:
            generator = self.teardowns.pop()
            with contextlib.suppress(StopIteration):
                next(generator)

    def names(self) -> set[str]:
        return {*self.values, *self.registry.fixtures}

    def get(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        entry = self.registry.fixtures.get(name)
        if entry is None:
            raise PreconditionError(f"no fixture named {name!r}")
        if name in self.resolving:
            chain = " → ".join([*self.resolving, name])
            raise PreconditionError(f"fixtures ask for each other in a circle: {chain}")
        self.resolving.append(name)
        try:
            result = entry.function(**self.arguments(entry.function))
        finally:
            self.resolving.pop()
        if inspect.isgenerator(result):
            value = next(result)
            self.teardowns.append(result)
        else:
            value = result
        self.values[name] = value
        return value

    def arguments(self, function: Callable[..., Any]) -> dict[str, Any]:
        """The fixtures `function` asks for, by its parameter names."""
        names = self.names()
        return {
            name: self.get(name) for name in inspect.signature(function).parameters if name in names
        }

    def call(self, function: Callable[..., Any], **values: Any) -> Any:
        return function(**self.arguments(function), **values)


class CommandFailed(SubjectFailedError):
    """A command `sh` ran said no; what it printed is the evidence."""

    def __init__(self, command: str, output: Output, characters: int) -> None:
        super().__init__(f"`{command}` exited {output.code}", output.excerpt(characters))


class Shell:
    """`sh`: run a command under supervision — bounded, logged, recorded on the
    call, its output condensed. A string runs in `sh -c`; a list runs as is.
    With `check` (the default) a non-zero exit ends the call: exit 1 with the
    output as evidence, or the machine's code when it timed out, hung or could
    not start. With `check=False` the `Output` comes back whatever happened."""

    def __init__(self, project: Project, call: Call) -> None:
        self.project = project
        self.call = call

    def __call__(
        self,
        command: str | list[str],
        *,
        check: bool = True,
        directory: str | Path | None = None,
        deadline: float | None = None,
        silence: float | None = None,
        environment: dict[str, str] | None = None,
        input: str | None = None,
    ) -> Output:
        argv = (
            ["sh", "-c", command] if isinstance(command, str) else [str(part) for part in command]
        )
        output = Supervisor(
            self.project,
            argv,
            directory=self.project.here / directory if directory else None,
            deadline=deadline,
            silence=silence,
            environment={**os.environ, **QUIET_ENVIRONMENT, **(environment or {})},
            call=self.call.id,
            stdin=input,
        ).run()
        if not check or output.ok:
            return output
        text = command if isinstance(command, str) else shlex.join(argv)
        if output.code == ExitCode.TIMED_OUT:
            raise TimedOutError(f"`{text}` {output.note} (log: {output.log})")
        if output.code == ExitCode.HUNG:
            raise HungError(f"`{text}` {output.note} (log: {output.log})")
        if output.code in (*NOT_RUNNABLE, ExitCode.PRECONDITIONS):
            raise PreconditionError(
                f"`{text}` could not run — {output.note or output.excerpt(400)}"
            )
        raise CommandFailed(text, output, int(self.project.configuration.get("output.characters")))


class Log:
    """`log`: notes for later — written to the call's log file, never to the
    caller, so they cost the agent nothing until someone looks."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, *parts: object) -> None:
        with self.path.open("a") as handle:
            handle.write(f"{now()} {' '.join(str(part) for part in parts)}\n")


@fixture
def root(project: Project) -> Path:
    """The working tree the call runs in."""
    return project.here


@fixture
def git(project: Project) -> Git:
    """git, in the working tree the call runs in."""
    return Git(project.here)


@fixture
def config(project: Project) -> Configuration:
    """The project's settings, by dotted key: `config.get("run.deadline")`."""
    return project.configuration


@fixture
def sh(project: Project, call: Call) -> Shell:
    """Run a command, supervised and condensed (see `Shell`)."""
    return Shell(project, call)


@fixture
def log(project: Project, call: Call) -> Log:
    """Notes to the call's log file."""
    return Log(project.logs / f"call-{call.id or 'unrecorded'}.log")


@fixture
def tmp() -> Iterator[Path]:
    """A fresh directory, removed after the call."""
    with tempfile.TemporaryDirectory(prefix="nock-") as directory:
        yield Path(directory)


@fixture
def changed(project: Project) -> list[str]:
    """Files changed in the working tree since it forked from the main branch,
    uncommitted ones included."""
    here = Git(project.here)
    base = here("merge-base", "HEAD", project.branch, check=False) or "HEAD"
    committed = here("diff", "--name-only", base, check=False).splitlines()
    return sorted({*committed, *here.dirty()})
