"""The toolbox: what the decorators register, and the registry that loads it —
nock's own built-ins first, then every module in the project's `.nock/tools/`
(a subdirectory, like `archive/`, is not loaded). A tool is live on the next
call; there is nothing to register by hand."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.commands import Command, command_name
from nock.errors import PreconditionError

if TYPE_CHECKING:
    from nock.project import Project

BUILT_IN_SOURCE = "nock"


class Event(StrEnum):
    """What a `@nock.hook` reacts to: Claude Code's hook events, by nock's names."""

    SESSION_START = "session-start"
    PROMPT = "prompt"
    AFTER_TOOL = "after-tool"
    TOOL_FAILED = "tool-failed"
    STOP = "stop"
    SESSION_END = "session-end"


# Claude Code's name for each event.
NATIVE_EVENTS = {
    Event.SESSION_START: "SessionStart",
    Event.PROMPT: "UserPromptSubmit",
    Event.AFTER_TOOL: "PostToolUse",
    Event.TOOL_FAILED: "PostToolUseFailure",
    Event.STOP: "Stop",
    Event.SESSION_END: "SessionEnd",
}


@dataclass
class Tool:
    """A command and where it came from."""

    command: Command
    source: str
    cache: list[str] = field(default_factory=list)
    pinned: bool = False


@dataclass
class Check:
    name: str
    function: Callable[..., Any]
    description: str
    source: str


@dataclass
class Hook:
    event: Event
    function: Callable[..., Any]
    name: str
    source: str


@dataclass
class Fixture:
    name: str
    function: Callable[..., Any]
    source: str


@dataclass
class Generator:
    """A file the project generates: `path` holds what `function` returns."""

    path: str
    function: Callable[..., Any]
    name: str
    source: str


@dataclass
class Pending:
    """What the decorators collected, before a registry claims it."""

    tools: list[Tool] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    hooks: list[Hook] = field(default_factory=list)
    fixtures: list[Fixture] = field(default_factory=list)
    generators: list[Generator] = field(default_factory=list)

    def take(self) -> Pending:
        taken = Pending(self.tools, self.checks, self.hooks, self.fixtures, self.generators)
        self.tools, self.checks, self.hooks, self.fixtures, self.generators = [], [], [], [], []
        return taken


PENDING = Pending()
# The source decorators stamp while a module is being imported.
CURRENT_SOURCE = [BUILT_IN_SOURCE]
BUILT_IN: list[Pending] = []


def summary_of(function: Callable[..., Any]) -> str:
    """A docstring's first sentence."""
    paragraph = (inspect.getdoc(function) or "").split("\n\n")[0].replace("\n", " ")
    sentence = re.split(r"(?<=[.!?])\s", paragraph, maxsplit=1)[0]
    return sentence.removesuffix(".")


def command(
    function: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    group: str | None = None,
    cache: str | list[str] | None = None,
    examples: list[str] | None = None,
    pinned: bool = False,
    rest: str | None = None,
    epilog: str | None = None,
) -> Any:
    """Make a function a command: `.nock/nock NAME` (or `.nock/nock GROUP NAME`).
    Its signature is its command line (see `nock.commands`), its docstring
    its help, and what it returns is printed as compactly as the shape allows
    (see `nock.render`).

    - `cache`: globs of the files its result depends on — called again with
      the same arguments while those files are unchanged, it replays the last
      result instead of running.
    - `examples`: argument strings that show how to call it; `--help` lists
      them and `nock check` runs each, so they cannot go stale.
    - `pinned`: reflection keeps it however rarely it is used."""

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        globs = cache.split() if isinstance(cache, str) else list(cache or [])
        built = Command(
            target,
            name or command_name(target),
            group=group,
            rest=rest,
            epilog=epilog,
            examples=list(examples or []),
            cached=bool(globs),
        )
        PENDING.tools.append(Tool(built, CURRENT_SOURCE[-1], globs, pinned))
        return target

    return decorate(function) if function is not None else decorate


def check(function: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
    """Make a function a check: `nock check` runs it, and so does reflection
    before it lands anything. It asks for fixtures by parameter name and
    returns its problems — strings, or `nock.Problem`s — and nothing when
    there are none. Raising `nock.PreconditionError` says the machine, not the
    code, is wrong."""

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        PENDING.checks.append(
            Check(name or command_name(target), target, summary_of(target), CURRENT_SOURCE[-1])
        )
        return target

    return decorate(function) if function is not None else decorate


def hook(event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """React to a Claude Code event (`session-start`, `prompt`, `after-tool`,
    `tool-failed`, `stop`, `session-end`). The hook asks for fixtures by name,
    `payload` among them (the event's JSON); text it returns is added to what
    the agent reads, and raising `nock.Block` at `stop` refuses the stop."""
    resolved = Event(event)

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        PENDING.hooks.append(Hook(resolved, function, function.__name__, CURRENT_SOURCE[-1]))
        return function

    return decorate


def fixture(function: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
    """Make a function a fixture: any command, check, hook or generator with a
    parameter of this name receives what it returns. A fixture may ask for
    other fixtures, and may `yield` its value to clean up after the call."""

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        PENDING.fixtures.append(Fixture(name or target.__name__, target, CURRENT_SOURCE[-1]))
        return target

    return decorate(function) if function is not None else decorate


def generator(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Make a function generate a file: `path` (relative to the repository)
    holds whatever text it returns. `nock gen` writes it; `nock check` fails
    when the file has drifted from it. It asks for fixtures by name."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        PENDING.generators.append(
            Generator(path, function, command_name(function), CURRENT_SOURCE[-1])
        )
        return function

    return decorate


class Registry:
    """Every tool, check, hook, fixture and generator, built-in or the project's."""

    def __init__(self) -> None:
        self.tools: list[Tool] = []
        self.checks: dict[str, Check] = {}
        self.hooks: list[Hook] = []
        self.fixtures: dict[str, Fixture] = {}
        self.generators: list[Generator] = []
        self.failures: list[str] = []

    @classmethod
    def load(cls, project: Project | None) -> Registry:
        registry = cls()
        registry.adopt(built_in())
        if project is not None:
            for path in sorted(project.code_path("tools").glob("*.py")):
                registry.import_module(path, project)
        names = registry.fixture_names()
        for tool in registry.tools:
            tool.command.fixtures = names
        return registry

    def adopt(self, pending: Pending) -> None:
        for tool in pending.tools:
            clash = self.tool(tool.command.path)
            if clash is not None:
                self.failures.append(
                    f"{tool.source}: `{tool.command.path}` is already defined in {clash.source}"
                )
                continue
            self.tools.append(tool)
        for check in pending.checks:
            self.checks[check.name] = check
        self.hooks += pending.hooks
        for entry in pending.fixtures:
            existing = self.fixtures.get(entry.name)
            if existing is not None and existing.source == BUILT_IN_SOURCE:
                self.failures.append(
                    f"{entry.source}: fixture `{entry.name}` is built in and cannot be replaced"
                )
                continue
            self.fixtures[entry.name] = entry
        self.generators += pending.generators

    def import_module(self, path: Path, project: Project) -> None:
        name = f"nock_tools.{path.stem}"
        source = (
            str(path.relative_to(project.here)) if path.is_relative_to(project.here) else str(path)
        )
        CURRENT_SOURCE.append(source)
        try:
            specification = importlib.util.spec_from_file_location(name, path)
            if specification is None or specification.loader is None:
                raise PreconditionError(f"cannot import {path}")
            module = importlib.util.module_from_spec(specification)
            sys.modules[name] = module
            specification.loader.exec_module(module)
        except Exception as error:
            PENDING.take()
            frames = [
                frame
                for frame in traceback.extract_tb(error.__traceback__)
                if frame.filename == str(path)
            ]
            where = f":{frames[-1].lineno}" if frames else ""
            self.failures.append(f"{source}{where}: {type(error).__name__}: {error}")
            return
        finally:
            CURRENT_SOURCE.pop()
        self.adopt(PENDING.take())

    def tool(self, path: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.command.path == path), None)

    def fixture_names(self) -> frozenset[str]:
        return frozenset({*self.fixtures, "project", "payload", "call"})

    def hooks_for(self, event: Event) -> list[Hook]:
        return [entry for entry in self.hooks if entry.event is event]

    def project_tools(self) -> list[Tool]:
        return [tool for tool in self.tools if tool.source != BUILT_IN_SOURCE]


def built_in() -> Pending:
    """nock's own tools. Their modules import this one for the decorators, so
    they are imported here, once, rather than at the top — a genuine cycle."""
    if not BUILT_IN:
        import nock.builtin
        import nock.fixtures  # noqa: F401

        BUILT_IN.append(PENDING.take())
    return BUILT_IN[0]
