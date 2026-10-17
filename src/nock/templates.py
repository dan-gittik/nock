"""Templates: text with Python in it, after auryn's twenty-line engine. The
program a template turns into prints its text lines; see SYNTAX."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from nock.errors import NotFoundError, PreconditionError

LINE = re.compile(r"^([ \t]*)(.*)$")
EXPRESSION = re.compile(r"\{\{(.*?)\}\}")
BLOCK = 4

SYNTAX = """\
Templates: a line starting with `!` is Python; a line ending with `:` opens a
block, whose lines are indented four more spaces (dropped from the output). A
blank line belongs to the block of the next line that is not blank. Any other
line is text, with `{{ expression }}` interpolated (`{{ value!r }}` for its
repr, as in an f-string); `\\!` starts a text line
with a literal `!`. `include("name")` renders another template in place.

    # Errors
    !for entry in entries:
        {{ entry.key.upper() }} = {{ entry.code }}
    !if not entries:
        # none yet

A catalog's templates see `entries` (each with its `key`, fields readable as
attributes), `catalog`, `catalogs` (every catalog's entries by name) and `tag`.\
"""


CONVERSIONS = {"r": "repr", "s": "str", "a": "ascii"}
CONVERSION = re.compile(r"^(.*?)!([rsa])$", re.DOTALL)


def interpolation(expression: str) -> str:
    """The Python for one `{{ expression }}`; a trailing `!r`, `!s` or `!a`
    converts the value as it would in an f-string."""
    expression = expression.strip()
    match = CONVERSION.match(expression)
    if match:
        return f"{CONVERSIONS[match.group(2)]}({match.group(1).strip()})"
    return f"({expression})"


class TemplateError(PreconditionError):
    """A template that does not compile or does not render, at its own line."""


class Template:
    def __init__(
        self, source: str, name: str = "<template>", library: Library | None = None
    ) -> None:
        self.source = source
        self.name = name
        self.library = library
        # The template line each generated line came from, to report errors where they are.
        self.origins: list[int] = []
        self.code = self.generate()
        self.filename = f"<template {name}>"
        try:
            self.compiled = compile(self.code, self.filename, "exec")
        except SyntaxError as error:
            raise TemplateError(f"{self.where(error.lineno)}: {error.msg}") from error

    def generate(self) -> str:
        stack: list[int] = []
        code: list[str] = []
        lines = self.source.splitlines()
        following = 0  # the indentation of the next line with content
        indents: list[int] = []
        for line in reversed(lines):
            if line.strip():
                following = len(line.expandtabs(BLOCK)) - len(line.expandtabs(BLOCK).lstrip())
            indents.append(following)
        indents.reverse()
        for number, line in enumerate(lines, 1):
            match = LINE.match(line)
            assert match
            content = match.group(2)
            # A blank line belongs to the block the next line with content is in.
            indent = indents[number - 1]
            while stack and indent < stack[-1] + BLOCK:
                stack.pop()
            depth = len(stack) * BLOCK
            prefix = " " * depth
            text_indent = max(indent - depth, 0)
            if content.startswith("!"):
                statement = content[1:].strip()
                if statement.endswith(":"):
                    code += [prefix + statement, prefix + " " * BLOCK + "pass"]
                    self.origins += [number, number]
                    stack.append(indent)
                else:
                    code.append(f"{prefix}_at({text_indent}); {statement}")
                    self.origins.append(number)
                continue
            if content.startswith("\\!"):
                content = content[1:]
            text = " " * text_indent + content if content else ""
            parts = EXPRESSION.split(text)
            arguments = [
                repr(part) if index % 2 == 0 else interpolation(part)
                for index, part in enumerate(parts)
                if part or index % 2
            ]
            code.append(f"{prefix}_emit({', '.join(arguments)})")
            self.origins.append(number)
        return "\n".join(code) + "\n"

    def where(self, generated_line: int | None) -> str:
        if generated_line and 0 < generated_line <= len(self.origins):
            return f"{self.name}:{self.origins[generated_line - 1]}"
        return self.name

    def render(self, **context: Any) -> str:
        output: list[str] = []
        indent = [0]

        def emit(*parts: object) -> None:
            output.append("".join(str(part) for part in parts))

        def at(column: int) -> None:
            indent[0] = column

        def include(name: str) -> None:
            if self.library is None:
                raise TemplateError(f"{self.name}: include({name!r}) needs a template library")
            column = indent[0]
            text = self.library.get(name).render(**context)
            for line in text.splitlines():
                output.append(" " * column + line if line else "")

        namespace: dict[str, Any] = {**context, "_emit": emit, "_at": at, "include": include}
        try:
            exec(self.compiled, namespace)
        except TemplateError:
            raise
        except Exception as error:
            raise TemplateError(f"{self.where(self.failing_line())}: {error!r}") from error
        text = "\n".join(output)
        return text + "\n" if self.source.endswith("\n") and text else text

    def failing_line(self) -> int | None:
        traceback = sys.exc_info()[2]
        line = None
        while traceback is not None:
            if traceback.tb_frame.f_code.co_filename == self.filename:
                line = traceback.tb_lineno
            traceback = traceback.tb_next
        return line


class Library:
    """Templates by name, from the first directory that has them."""

    def __init__(self, *directories: Path) -> None:
        self.directories = directories
        self.cache: dict[Path, tuple[float, Template]] = {}

    def find(self, name: str) -> Path:
        for directory in self.directories:
            path = directory / name
            if path.is_file():
                return path
        raise NotFoundError(
            f"no template {name} in {', '.join(str(directory) for directory in self.directories)}"
        )

    def get(self, name: str) -> Template:
        path = self.find(name)
        modified = path.stat().st_mtime
        cached = self.cache.get(path)
        if cached and cached[0] == modified:
            return cached[1]
        template = Template(path.read_text(), name, self)
        self.cache[path] = (modified, template)
        return template

    def render(self, name: str, **context: Any) -> str:
        return self.get(name).render(**context)
