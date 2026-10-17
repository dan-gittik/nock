"""Commands from function signatures, on argparse. The signature is the
interface:

- a positional parameter is an argument, a keyword-only one an `--option`
  (a bool is a flag, a list repeats, an enum or Literal gives the choices);
- a parameter typed as a record — a dataclass, a TypedDict, or a dict — is one
  JSON argument (inline, a path to a JSON file, or JSON on stdin), checked
  against the schema derived from the type, which `--help` prints;
- a parameter named after a fixture is not on the command line at all: the
  framework supplies it (see `nock.fixtures`);
- the docstring is the help; `Annotated[T, Option("--flag", help=…)]` renames
  or describes an option.

    @nock.command
    def failures(sh, *, path: str = "tests") -> list[dict]:
        \"\"\"The failing tests, with their first error line.\"\"\""""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import enum
import inspect
import json
import re
import sys
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nock.errors import UsageError
from nock.schema import validate

DESTINATION = "_nock_command"
# Flags every command takes, whatever its signature.
JSON_FLAG = "_json"
FULL_FLAG = "_full"
FRESH_FLAG = "_fresh"
INVALID_CHOICE = re.compile(r"argument COMMAND: invalid choice: '([^']*)'")
UNRECOGNIZED = "unrecognized arguments: "
TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Option:
    """How a parameter appears on the command line, for `Annotated`."""

    flags: tuple[str, ...] = ()
    help: str = ""

    def __init__(self, *flags: str, help: str = "") -> None:
        self.flags = flags
        self.help = help


class Parser(argparse.ArgumentParser):
    """argparse, with a usage error that is nock's exit 40 rather than 2."""

    def error(self, message: str) -> typing.NoReturn:
        raise UsageError(f"{message}\n{self.format_usage().rstrip()}")


class Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32, width=100)


def unwrap(annotation: Any) -> tuple[Any, Option, bool]:
    """The bare type, its Option, and whether None is allowed."""
    option = Option()
    if typing.get_origin(annotation) is typing.Annotated:
        annotation, *extras = typing.get_args(annotation)
        option = next((extra for extra in extras if isinstance(extra, Option)), option)
    optional = False
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        members = [member for member in typing.get_args(annotation) if member is not type(None)]
        optional = len(members) < len(typing.get_args(annotation))
        annotation = members[0] if len(members) == 1 else str
    return annotation, option, optional


def is_record(annotation: Any) -> bool:
    """Whether a parameter of this type is passed as one JSON argument."""
    return (
        annotation is dict
        or typing.get_origin(annotation) is dict
        or typing.is_typeddict(annotation)
        or (isinstance(annotation, type) and dataclasses.is_dataclass(annotation))
    )


def schema_of(annotation: Any) -> dict[str, Any]:
    """The JSON schema a type describes, as far as the subset nock checks goes."""
    annotation, _, optional = unwrap(annotation)
    schema = shape_of(annotation)
    return {"anyOf": [schema, {"type": "null"}]} if optional and schema else schema


def shape_of(annotation: Any) -> dict[str, Any]:
    origin = typing.get_origin(annotation)
    if annotation in TYPES:
        return {"type": TYPES[annotation]}
    if origin is typing.Literal:
        return {"enum": list(typing.get_args(annotation))}
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {"enum": [member.value for member in annotation]}
    if annotation is list or origin in (list, tuple):
        arguments = typing.get_args(annotation)
        return {"type": "array", **({"items": schema_of(arguments[0])} if arguments else {})}
    if typing.is_typeddict(annotation):
        # With postponed annotations only the hints know which keys are NotRequired.
        hints = typing.get_type_hints(annotation, include_extras=True)
        total = getattr(annotation, "__total__", True)
        required, properties = [], {}
        for name, hint in hints.items():
            wrapper = typing.get_origin(hint)
            if wrapper in (typing.NotRequired, typing.Required):
                hint = typing.get_args(hint)[0]
            if wrapper is typing.Required or (total and wrapper is not typing.NotRequired):
                required.append(name)
            properties[name] = schema_of(hint)
        return {
            "type": "object",
            "required": required,
            "additionalProperties": False,
            "properties": properties,
        }
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        hints = typing.get_type_hints(annotation)
        fields = dataclasses.fields(annotation)
        return {
            "type": "object",
            "required": [
                item.name
                for item in fields
                if item.default is dataclasses.MISSING
                and item.default_factory is dataclasses.MISSING
            ],
            "additionalProperties": False,
            "properties": {item.name: schema_of(hints[item.name]) for item in fields},
        }
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def schema_help(schema: dict[str, Any]) -> str:
    """A record schema as `--help` prints it."""
    lines = ["Record (JSON, a path to a JSON file, or JSON on stdin):"]
    required = set(schema.get("required", []))
    for name, field_schema in schema.get("properties", {}).items():
        lines.append(
            f"  {name}: {describe(field_schema)}" + ("" if name in required else " (optional)")
        )
    return "\n".join(lines)


def describe(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return " or ".join(describe(option) for option in schema["anyOf"])
    if "enum" in schema:
        return "one of " + "|".join(map(str, schema["enum"]))
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list of {describe(schema.get('items', {}))}"
    if kind == "object" and "properties" in schema:
        return "{" + ", ".join(schema["properties"]) + "}"
    description = schema.get("description")
    return f"{kind} — {description}" if description else str(kind)


def read_record(argument: str | None) -> Any:
    """A record: inline JSON, a path to a file holding it, or — when the
    argument is omitted or `-` — JSON on stdin."""
    if argument is None or argument == "-":
        if sys.stdin.isatty():
            raise UsageError("expected a JSON argument, a path to a JSON file, or JSON on stdin")
        text = sys.stdin.read()
    elif not argument.lstrip().startswith(("{", "[")) and Path(argument).is_file():
        text = Path(argument).read_text()
    else:
        text = argument
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise UsageError(f"not valid JSON: {error}") from error


class Kind(enum.StrEnum):
    FIXTURE = "fixture"
    RECORD = "record"
    ARGUMENT = "argument"
    OPTION = "option"
    REST = "rest"


@dataclass
class Parameter:
    name: str
    kind: Kind
    annotation: Any
    option: Option
    optional: bool
    default: Any


@dataclass
class Command:
    function: Callable[..., Any]
    name: str
    group: str | None = None
    # The parameter that takes every remaining word verbatim (`nock run -- …`).
    rest: str | None = None
    epilog: str | None = None
    examples: list[str] = field(default_factory=list)
    cached: bool = False
    fixtures: frozenset[str] = frozenset()

    @property
    def path(self) -> str:
        """The words that name it: `failures`, `gen add`."""
        return f"{self.group} {self.name}" if self.group else self.name

    @property
    def summary(self) -> str:
        """The docstring's first sentence."""
        paragraph = (inspect.getdoc(self.function) or "").split("\n\n")[0].replace("\n", " ")
        return re.split(r"(?<=[.!?])\s", paragraph, maxsplit=1)[0].removesuffix(".")

    def parameters(self) -> list[Parameter]:
        hints = typing.get_type_hints(self.function, include_extras=True)
        found = []
        for parameter in inspect.signature(self.function).parameters.values():
            annotation, option, optional = unwrap(hints.get(parameter.name, str))
            default = parameter.default
            if parameter.name in self.fixtures:
                kind = Kind.FIXTURE
            elif parameter.name == self.rest:
                kind = Kind.REST
            elif is_record(annotation):
                kind = Kind.RECORD
            elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                kind = Kind.OPTION
            else:
                kind = Kind.ARGUMENT
            found.append(Parameter(parameter.name, kind, annotation, option, optional, default))
        return found

    def usage(self) -> str:
        """The command line in a few words: `failures [--path PATH]`."""
        words = [self.path]
        for parameter in self.parameters():
            missing = parameter.default is inspect.Parameter.empty
            metavar = parameter.name.upper()
            if parameter.kind is Kind.RECORD:
                words.append("JSON" if missing and not parameter.optional else "[JSON]")
            elif parameter.kind is Kind.REST:
                words.append(f"-- {metavar}…")
            elif parameter.kind is Kind.ARGUMENT:
                repeated = typing.get_origin(parameter.annotation) in (list, tuple)
                word = f"{metavar}…" if repeated else metavar
                words.append(word if missing and not parameter.optional else f"[{word}]")
            elif parameter.kind is Kind.OPTION:
                flag = (parameter.option.flags or (option_flag(parameter.name),))[0]
                word = flag if parameter.annotation is bool else f"{flag} {metavar}"
                words.append(word if missing else f"[{word}]")
        return " ".join(words)

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.description = inspect.getdoc(self.function)
        epilog = [self.epilog] if self.epilog else []
        for parameter in self.parameters():
            if parameter.kind is Kind.RECORD:
                schema = schema_of(parameter.annotation)
                if schema.get("properties"):
                    epilog.insert(0, schema_help(schema))
            if parameter.kind is not Kind.FIXTURE:
                self.add(parser, parameter)
        if self.examples:
            epilog.append(
                "Examples:\n"
                + "\n".join(f"  .nock/nock {self.path} {example}" for example in self.examples)
            )
        parser.epilog = "\n\n".join(epilog) or None
        parser.add_argument(
            "--json", action="store_true", dest=JSON_FLAG, help="Print the result as JSON."
        )
        parser.add_argument(
            "--full", action="store_true", dest=FULL_FLAG, help="Print all of it, uncapped."
        )
        if self.cached:
            parser.add_argument(
                "--fresh", action="store_true", dest=FRESH_FLAG, help="Ignore the cached result."
            )
        parser.set_defaults(**{DESTINATION: self})

    def add(self, parser: argparse.ArgumentParser, parameter: Parameter) -> None:
        annotation, option = parameter.annotation, parameter.option
        has_default = parameter.default is not inspect.Parameter.empty
        settings: dict[str, Any] = {"help": option.help or None}
        if parameter.kind is Kind.RECORD:
            settings["metavar"] = "JSON"
            settings["nargs"] = "?"
            parser.add_argument(parameter.name, **settings)
            return
        if parameter.kind is Kind.REST:
            settings["metavar"] = parameter.name.upper()
            settings["nargs"] = argparse.REMAINDER
            parser.add_argument(parameter.name, **settings)
            return
        repeated = typing.get_origin(annotation) in (list, tuple)
        kind = typing.get_args(annotation)[0] if repeated else annotation
        if isinstance(kind, type) and issubclass(kind, enum.Enum):
            settings["choices"] = [member.value for member in kind]
            settings["type"] = str
        elif typing.get_origin(kind) is typing.Literal:
            settings["choices"] = list(typing.get_args(kind))
            settings["type"] = type(settings["choices"][0])
        elif kind in (int, float):
            settings["type"] = kind
        if parameter.kind is Kind.OPTION:
            flags = option.flags or (option_flag(parameter.name),)
            if kind is bool:
                parser.add_argument(
                    *flags, action="store_true", dest=parameter.name, help=settings["help"]
                )
                return
            settings["dest"] = parameter.name
            if repeated:
                settings["action"] = "append"
                settings["default"] = []
            elif has_default:
                settings["default"] = parameter.default
                if parameter.default is not None and settings["help"] is not None:
                    settings["help"] += f" (default: {parameter.default})"
            else:
                settings["required"] = True
            settings["metavar"] = parameter.name.upper()
            parser.add_argument(*flags, **settings)
            return
        settings["metavar"] = parameter.name.upper()
        if repeated:
            settings["nargs"] = "*" if has_default else "+"
        elif has_default or parameter.optional:
            settings["nargs"] = "?"
            settings["default"] = parameter.default if has_default else None
        parser.add_argument(parameter.name, **settings)

    def values(self, namespace: argparse.Namespace) -> dict[str, Any]:
        """The command-line values, converted to what the function's types ask for."""
        given = {name: value for name, value in vars(namespace).items() if not name.startswith("_")}
        values: dict[str, Any] = {}
        for parameter in self.parameters():
            if parameter.kind is Kind.FIXTURE or parameter.name not in given:
                continue
            value = given[parameter.name]
            if parameter.kind is Kind.REST and value and value[0] == "--":
                value = value[1:]
            elif parameter.kind is Kind.RECORD:
                value = self.record(parameter, value)
            elif value is not None:
                repeated = typing.get_origin(parameter.annotation) in (list, tuple)
                kind = (
                    typing.get_args(parameter.annotation)[0] if repeated else parameter.annotation
                )
                if isinstance(kind, type) and issubclass(kind, enum.Enum):
                    value = [kind(item) for item in value] if repeated else kind(value)
            values[parameter.name] = value
        return values

    def record(self, parameter: Parameter, argument: str | None) -> Any:
        if (
            argument is None
            and parameter.default is not inspect.Parameter.empty
            and sys.stdin.isatty()
        ):
            return parameter.default
        value = read_record(argument)
        validate(value, schema_of(parameter.annotation))
        annotation = parameter.annotation
        if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
            return annotation(**value)
        return value


def option_flag(name: str) -> str:
    return "--" + name.replace("_", "-")


class Group:
    def __init__(self, name: str, help: str = "") -> None:
        self.name = name
        self.help = help
        self.members: dict[str, Command | Group] = {}

    @property
    def summary(self) -> str:
        return self.help

    def group(self, name: str, help: str = "") -> Group:
        found = self.members.get(name)
        if isinstance(found, Group):
            return found
        created = Group(name, help)
        self.members[name] = created
        return created

    def add(self, command: Command) -> None:
        if command.group:
            self.group(command.group).members[command.name] = command
        else:
            self.members[command.name] = command

    def commands(self) -> list[Command]:
        found = []
        for member in self.members.values():
            found += member.commands() if isinstance(member, Group) else [member]
        return found

    def configure(self, parser: argparse.ArgumentParser, depth: int = 0) -> None:
        parser.description = self.help
        parser.set_defaults(**{DESTINATION: self})
        commands = parser.add_subparsers(title="commands", metavar="COMMAND", dest=f"_group{depth}")
        for name, member in sorted(self.members.items()):
            child = commands.add_parser(
                name,
                help=member.summary,
                formatter_class=Formatter,
                add_help=False,
                allow_abbrev=False,
            )
            child.add_argument("--help", action="help", help="Show this help and exit.")
            if isinstance(member, Group):
                member.configure(child, depth + 1)
            else:
                member.configure(child)

    def parser(self) -> Parser:
        parser = Parser(
            prog=self.name, formatter_class=Formatter, add_help=False, allow_abbrev=False
        )
        parser.add_argument("--help", action="help", help="Show this help and exit.")
        self.configure(parser)
        return parser

    def parse(self, arguments: list[str]) -> tuple[Command | Group, argparse.Namespace]:
        """The command the words name and its parsed values, or a usage error
        that says what was probably meant."""
        parser = self.parser()
        try:
            namespace, extras = parser.parse_known_args(arguments)
        except UsageError as error:
            raise UsageError(self.suggest(str(error), arguments)) from error
        target = getattr(namespace, DESTINATION)
        if extras:
            raise UsageError(self.unrecognized(target, extras))
        return target, namespace

    def suggest(self, message: str, arguments: list[str]) -> str:
        match = INVALID_CHOICE.search(message)
        if not match:
            return message
        word = match.group(1)
        known = [command.path for command in self.commands()]
        typed = " ".join(part for part in arguments if not part.startswith("-"))[: len(word) + 40]
        close = difflib.get_close_matches(typed, known, 3, 0.5) or difflib.get_close_matches(
            word, [*known, *(path.split()[-1] for path in known)], 3, 0.5
        )
        hint = f" — did you mean {' or '.join(f'`{name}`' for name in close)}?" if close else ""
        return f"no command `{word}`{hint} (`.nock/nock` lists them all)"

    def unrecognized(self, target: Command | Group, extras: list[str]) -> str:
        if not isinstance(target, Command):
            return f"unrecognized arguments: {' '.join(extras)}"
        known = [
            flag
            for parameter in target.parameters()
            if parameter.kind is Kind.OPTION
            for flag in (parameter.option.flags or (option_flag(parameter.name),))
        ]
        hints = []
        for extra in extras:
            if extra.startswith("-"):
                close = difflib.get_close_matches(
                    extra.split("=")[0], [*known, "--json", "--full"], 1, 0.5
                )
                if close:
                    hints.append(f"{extra} → did you mean {close[0]}?")
        usage = f"usage: .nock/nock {target.usage()}"
        return "; ".join([f"unrecognized arguments: {' '.join(extras)}", *hints]) + f"\n{usage}"


def command_name(function: Callable[..., Any]) -> str:
    return function.__name__.rstrip("_").replace("_", "-")
