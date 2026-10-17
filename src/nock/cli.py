"""The weaver: one call, from the words typed to the exit code. It finds the
command, hands it its fixtures, renders what it returns, caps what is too long
(the rest goes to a file), notices a failure repeating itself, and records the
call — so a tool's author writes only what the tool does."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nock import hooks
from nock.commands import FRESH_FLAG, FULL_FLAG, JSON_FLAG, Command, Group
from nock.errors import Error, Exit, ExitCode, UsageError
from nock.fixtures import Call, Scope
from nock.processes import Output
from nock.project import Project
from nock.render import PACKAGE_INTERNAL, as_json, capped, crash, plain, render
from nock.telemetry import UNRECORDED_VARIABLE, Cache, Calls, digest, tree_state
from nock.tools import BUILT_IN_SOURCE, Registry, Tool

VERSION = "0.2.0"
# Commands that work without a project.
PROJECTLESS = ("init",)
FOOTER = (
    "Exit codes: 0 yes · 1 no, the subject failed · 40–49 change the request (42: fix the "
    "machine or the state, not the code) · 50–56 retry or escalate. Any command takes --json "
    "for JSON and --full for uncapped output; `.nock/nock COMMAND --help` shows its usage."
)


def main() -> None:
    arguments = sys.argv[1:]
    if arguments[:1] == ["hook"]:
        sys.exit(hooks.run(arguments[1] if len(arguments) > 1 else "", sys.stdin.read()))
    sys.exit(Invocation(arguments).run())


def build(registry: Registry) -> Group:
    root = Group("nock", "nock: this project's toolbox.")
    for tool in registry.tools:
        root.add(tool.command)
    return root


def listing(registry: Registry) -> str:
    """The toolbox in a line per command, the project's own first."""
    sections = []
    own = registry.project_tools()
    if own:
        sections.append("Project tools:\n" + "\n".join(f"  {line(tool)}" for tool in own))
    built_in = [tool for tool in registry.tools if tool.source == BUILT_IN_SOURCE]
    sections.append("nock:\n" + "\n".join(f"  {line(tool)}" for tool in built_in))
    return "\n\n".join([*sections, FOOTER])


def line(tool: Tool) -> str:
    summary = tool.command.summary
    return f"{tool.command.usage()}" + (f" — {summary}" if summary else "")


@dataclass
class Outcome:
    """How a call ended: what it returned (or the evidence it failed with),
    its exit code, what went wrong — for telemetry, when it was an error rather
    than an answer — and the notes to print after the output."""

    result: Any
    code: int
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class Invocation:
    def __init__(self, arguments: list[str]) -> None:
        self.arguments = arguments
        self.project: Project | None = None
        self.started = time.monotonic()

    def run(self) -> int:
        try:
            self.project = Project.discover()
        except Error as error:
            if not self.arguments or self.arguments[0] not in PROJECTLESS:
                if self.arguments in ([], ["--help"]):
                    print(listing(Registry.load(None)))
                    return ExitCode.SUCCESS
                sys.stderr.write(f"nock: {error}\n")
                return error.exit_code
        registry = self.project.registry if self.project else Registry.load(None)
        for failure in registry.failures:
            sys.stderr.write(f"nock: a tool did not load — {failure}\n")
        if self.arguments in ([], ["--help"]):
            print(listing(registry))
            return ExitCode.SUCCESS
        if self.arguments == ["--version"]:
            print(f"nock {VERSION}")
            return ExitCode.SUCCESS
        try:
            target, namespace = build(registry).parse(self.arguments)
        except UsageError as error:
            self.record_refusal(self.attempted(registry), error)
            return error.exit_code
        if isinstance(target, Group):
            print(self.group_listing(registry, target))
            return ExitCode.SUCCESS
        tool = registry.tool(target.path)
        assert tool is not None
        return self.invoke(registry, tool, target, namespace)

    def attempted(self, registry: Registry) -> str:
        """The command a refused call meant: the known one its words name, or
        the word that named nothing."""
        known = {tool.command.path for tool in registry.tools}
        two = " ".join(self.arguments[:2])
        if two in known:
            return two
        return self.arguments[0] if self.arguments else "?"

    def group_listing(self, registry: Registry, group: Group) -> str:
        tools = [registry.tool(command.path) for command in group.commands()]
        return "\n".join(line(tool) for tool in tools if tool is not None)

    def words(self, command: Command) -> list[str]:
        """The arguments after the words that name the command."""
        return self.arguments[len(command.path.split()) :]

    def record_refusal(self, attempted: str, error: Error) -> None:
        sys.stderr.write(f"nock: {error}\n")
        if self.project is None:
            return
        calls = Calls(self.project)
        call = calls.start(attempted, self.arguments, None)
        calls.finish(
            call,
            {
                "duration": time.monotonic() - self.started,
                "exit_code": error.exit_code,
                "error": str(error).split("\n")[0][:300],
                "output_bytes": 0,
                "output_lines": 0,
                "full_bytes": 0,
            },
        )

    def invoke(self, registry: Registry, tool: Tool, command: Command, namespace: Any) -> int:
        project = self.project
        words = self.words(command)
        options = vars(namespace)
        calls = Calls(project) if project and not os.environ.get(UNRECORDED_VARIABLE) else None
        call = calls.start(command.path, words, tool.source) if calls else None
        cache = Cache(project) if project and tool.cache else None
        key = (
            cache.key(tool, [word for word in words if word not in ("--full", "--fresh")])
            if cache
            else None
        )
        cached = cache.get(key) if cache and key and not options.get(FRESH_FLAG) else None
        if cached is not None:
            outcome = Outcome(self.replayed(cached), cached["code"])
            outcome.notes.append(
                f"cached: nothing it depends on changed since call {cached['call']} (--fresh reruns it)"
            )
        else:
            outcome = self.execute(registry, command, namespace, call)
            if (
                cache
                and key
                and outcome.error is None
                and outcome.code in (ExitCode.SUCCESS, ExitCode.SUBJECT_FAILED)
            ):
                cache.put(key, self.cacheable(outcome.result, outcome.code, call))
        printed, full = self.output(outcome.result, options, call)
        if printed:
            sys.stdout.write(printed + "\n")
            sys.stdout.flush()
        fingerprint = digest(full)
        tree = None
        if outcome.code != ExitCode.SUCCESS and project and calls and call:
            tree = tree_state(project.here)
            repeat = calls.repeat(call, command.path, words, fingerprint, tree)
            if repeat:
                outcome.notes.append(repeat)
        for note in outcome.notes:
            sys.stderr.write(f"nock: {note}\n")
        if calls and call:
            calls.finish(
                call,
                {
                    "duration": round(time.monotonic() - self.started, 3),
                    "exit_code": outcome.code,
                    "output_bytes": len(printed.encode()),
                    "output_lines": printed.count("\n") + 1 if printed else 0,
                    "full_bytes": len(full.encode()),
                    "cached": int(cached is not None),
                    "error": outcome.error.split("\n")[0][:300] if outcome.error else None,
                    "fingerprint": fingerprint,
                    "tree": tree,
                },
            )
        return outcome.code

    def execute(
        self, registry: Registry, command: Command, namespace: Any, call: int | None
    ) -> Outcome:
        """Run the command with its fixtures; whatever happens becomes an outcome."""
        values = {"project": self.project, "call": Call(call, command.path)}
        try:
            with Scope(registry, values) as scope:
                result = scope.call(command.function, **command.values(namespace))
        except Exit as exit:
            return Outcome(None, exit.code)
        except Error as error:
            outcome = Outcome(error.value, error.exit_code, notes=[str(error)])
            if error.value is None:
                outcome.error = str(error)
            return outcome
        except KeyboardInterrupt:
            return Outcome(None, 130, "interrupted")
        except Exception as error:
            report = crash(error, PACKAGE_INTERNAL)
            return Outcome(
                None,
                ExitCode.INTERNAL,
                report.split("\n")[0],
                [f"`{command.path}` crashed — {report}"],
            )
        if isinstance(result, Output):
            return Outcome(result, result.code)
        return Outcome(result, ExitCode.SUBJECT_FAILED if result is False else ExitCode.SUCCESS)

    def replayed(self, cached: dict[str, Any]) -> Any:
        if cached["kind"] == "output":
            return Output(cached["code"], cached["text"].split("\n"), Path(cached["log"]))
        return cached["value"]

    def cacheable(self, result: Any, code: int, call: int | None) -> dict[str, Any]:
        if isinstance(result, Output):
            return {
                "kind": "output",
                "text": result.text,
                "log": str(result.log),
                "code": code,
                "call": call,
            }
        return {"kind": "value", "value": plain(result), "code": code, "call": call}

    def output(self, result: Any, options: dict[str, Any], call: int | None) -> tuple[str, str]:
        """What to print, and the whole of it."""
        if options.get(JSON_FLAG):
            if isinstance(result, Output):
                text = json.dumps(
                    {"exit_code": result.code, "output": result.text, "log": str(result.log)},
                    ensure_ascii=False,
                )
            else:
                text = as_json(result) if result is not None else ""
            return text, text
        if isinstance(result, Output):
            full = result.text
            if options.get(FULL_FLAG):
                return full, full
            return result.excerpt(self.characters()), full
        full = render(result)
        if options.get(FULL_FLAG) or not full:
            return full, full
        project = self.project
        rows = int(project.configuration.get("output.rows")) if project else 60
        text, cut = capped(full, self.characters(), rows)
        if cut and project:
            path = project.local / "outputs" / f"call-{call or 'unrecorded'}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(full + "\n")
            text += f"\n(all of it: {path.relative_to(project.here) if path.is_relative_to(project.here) else path}, or --full)"
        return text, full

    def characters(self) -> int:
        return int(self.project.configuration.get("output.characters")) if self.project else 8000
