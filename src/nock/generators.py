"""Generators: files the project derives rather than writes — and so can never
drift from their source. Two kinds:

- a `@nock.generator(path)` function returns the text of one file, from
  whatever it reads (a schema, a spec, the code itself);
- a catalog is a central ledger the code is generated from — error codes,
  feature flags, UI strings: a directory under `.nock/generators/` holding the
  schema of one entry, the entries, and templates (`nock.templates`) bound to
  the files they render. Every write re-renders its outputs.

`nock gen` writes every generated file; a check fails when one has drifted."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.errors import ConflictError, Error, NotFoundError, PreconditionError, UsageError
from nock.fixtures import Call, Scope
from nock.schema import Validator, check_schema, validate
from nock.templates import Library
from nock.utils import read_json, slug, write_atomic, write_json

if TYPE_CHECKING:
    from nock.project import Project
    from nock.tools import Generator

BINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["template", "file"],
    "additionalProperties": False,
    "properties": {
        "template": {"type": "string", "description": "A file under the catalog's templates/."},
        "file": {"type": "string", "description": "The output, relative to the repository root."},
        "tag": {"type": "string", "description": "Render only entries whose `tags` include it."},
    },
}


@dataclass
class Binding:
    template: str
    file: str
    tag: str | None = None


class Catalog:
    def __init__(self, project: Project, name: str) -> None:
        self.project = project
        self.name = name

    @property
    def directory(self) -> Path:
        return self.project.code_path("generators", self.name)

    @property
    def schema_file(self) -> Path:
        return self.directory / "schema.json"

    @property
    def entries_file(self) -> Path:
        return self.directory / "entries.json"

    @property
    def outputs_file(self) -> Path:
        return self.directory / "outputs.toml"

    def exists(self) -> bool:
        return self.schema_file.is_file()

    def schema(self) -> dict[str, Any]:
        return read_json(self.schema_file)

    def entries(self) -> dict[str, dict[str, Any]]:
        return read_json(self.entries_file) if self.entries_file.is_file() else {}

    def bindings(self) -> list[Binding]:
        if not self.outputs_file.is_file():
            return []
        data = tomllib.loads(self.outputs_file.read_text())
        return [
            Binding(item["template"], item["file"], item.get("tag"))
            for item in data.get("output", [])
        ]

    # --- validation ---------------------------------------------------------------

    def problems(self, entries: dict[str, dict[str, Any]] | None = None) -> list[str]:
        entries = self.entries() if entries is None else entries
        schema = self.schema()
        return [
            f"{self.name}.{key}: {problem}".replace("(root)", "(entry)")
            for key, entry in entries.items()
            for problem in Validator().problems(entry, schema)
        ]

    def validated(self, entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        problems = self.problems(entries)
        if problems:
            raise UsageError("; ".join(problems))
        return dict(sorted(entries.items()))

    # --- writing ---------------------------------------------------------------------

    def save(self, entries: dict[str, dict[str, Any]]) -> list[Path]:
        """Write the entries and re-render every output; returns what changed."""
        write_json(self.entries_file, self.validated(entries))
        return [self.entries_file, *self.render()]

    def add(self, record: dict[str, Any]) -> list[Path]:
        key = record.get("key")
        if not isinstance(key, str) or not key:
            raise UsageError("an entry needs a `key`")
        entries = self.entries()
        if key in entries:
            raise ConflictError(f"{self.name} already has {key!r} — set it instead")
        entries[key] = {name: value for name, value in record.items() if name != "key"}
        return self.save(entries)

    def set(self, key: str, change: dict[str, Any]) -> list[Path]:
        """Merge `change` into an entry; a null removes a field."""
        entries = self.entries()
        if key not in entries:
            raise NotFoundError(f"{self.name} has no {key!r}")
        merged = {**entries[key], **change}
        entries[key] = {name: value for name, value in merged.items() if value is not None}
        return self.save(entries)

    def remove(self, key: str) -> list[Path]:
        entries = self.entries()
        if entries.pop(key, None) is None:
            raise NotFoundError(f"{self.name} has no {key!r}")
        return self.save(entries)

    def bind(self, record: dict[str, Any]) -> list[Path]:
        validate(record, BINDING_SCHEMA)
        if not (self.directory / "templates" / record["template"]).is_file():
            raise NotFoundError(
                f"no template {record['template']} in {self.directory / 'templates'}"
            )
        bindings = [binding for binding in self.bindings() if binding.file != record["file"]]
        bindings.append(Binding(record["template"], record["file"], record.get("tag")))
        self.write_bindings(bindings)
        return [self.outputs_file, *self.render()]

    def unbind(self, file: str) -> list[Path]:
        bindings = [binding for binding in self.bindings() if binding.file != file]
        self.write_bindings(bindings)
        return [self.outputs_file]

    def write_bindings(self, bindings: list[Binding]) -> None:
        blocks = []
        for binding in bindings:
            lines = [
                "[[output]]",
                f"template = {json.dumps(binding.template)}",
                f"file = {json.dumps(binding.file)}",
            ]
            if binding.tag:
                lines.append(f"tag = {json.dumps(binding.tag)}")
            blocks.append("\n".join(lines))
        write_atomic(self.outputs_file, "\n\n".join(blocks) + "\n" if blocks else "")

    # --- rendering -------------------------------------------------------------------

    def rendered(self, root: Path | None = None) -> dict[Path, str]:
        """Each bound output's path and the text it should hold."""
        root = root or self.project.here
        entries = self.entries()
        catalogs = {
            catalog.name: listed(catalog.entries()) for catalog in Catalogs(self.project).all()
        }
        outputs = {}
        library = Library(self.directory / "templates")
        for binding in self.bindings():
            chosen = {
                key: entry
                for key, entry in entries.items()
                if binding.tag is None or binding.tag in (entry.get("tags") or [])
            }
            outputs[root / binding.file] = library.render(
                binding.template,
                entries=listed(chosen),
                catalog=self.name,
                catalogs=catalogs,
                tag=binding.tag,
            )
        return outputs

    def render(self) -> list[Path]:
        written = []
        for path, text in self.rendered(self.project.here).items():
            if not path.is_file() or path.read_text() != text:
                write_atomic(path, text)
                written.append(path)
        return written

    def drifted(self, root: Path) -> list[str]:
        return [
            str(path.relative_to(root))
            for path, text in self.rendered(root).items()
            if not path.is_file() or path.read_text() != text
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema": self.schema(),
            "entries": self.entries(),
            "outputs": [binding.__dict__ for binding in self.bindings()],
        }


class Entry(dict[str, Any]):
    """An entry as templates see it: its fields read as attributes too
    (`entry.code`), and a field it lacks reads as None."""

    def __getattr__(self, name: str) -> Any:
        return self.get(name)


def listed(entries: dict[str, dict[str, Any]]) -> list[Entry]:
    return [Entry(key=key, **entry) for key, entry in sorted(entries.items())]


class Catalogs:
    def __init__(self, project: Project) -> None:
        self.project = project

    @property
    def directory(self) -> Path:
        return self.project.code_path("generators")

    def all(self) -> list[Catalog]:
        if not self.directory.is_dir():
            return []
        return [
            Catalog(self.project, path.name)
            for path in sorted(self.directory.iterdir())
            if (path / "schema.json").is_file()
        ]

    def get(self, name: str) -> Catalog:
        catalog = Catalog(self.project, name)
        if not catalog.exists():
            raise NotFoundError(f"no catalog {name!r} — `nock gen new {name} SCHEMA` creates it")
        return catalog

    def new(self, name: str, schema: dict[str, Any]) -> Catalog:
        name = slug(name)
        catalog = Catalog(self.project, name)
        if catalog.exists():
            raise ConflictError(f"catalog {name!r} exists")
        check_schema(schema)
        write_json(catalog.schema_file, schema)
        write_json(catalog.entries_file, {})
        (catalog.directory / "templates").mkdir(parents=True, exist_ok=True)
        return catalog


class Generators:
    """Every generated file, from functions and catalogs alike."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def outputs(self, root: Path | None = None) -> dict[Path, str]:
        """Each generated file's path and the text it should hold."""
        root = root or self.project.here
        outputs: dict[Path, str] = {}
        for catalog in Catalogs(self.project).all():
            outputs.update(catalog.rendered(root))
        for entry in self.project.registry.generators:
            outputs[root / entry.path] = self.generate(entry)
        return outputs

    def generate(self, entry: Generator) -> str:
        values = {"project": self.project, "call": Call(None, f"generate {entry.name}")}
        try:
            with Scope(self.project.registry, values) as scope:
                text = scope.call(entry.function)
        except Error:
            raise
        except Exception as error:
            raise PreconditionError(
                f"generator {entry.name} ({entry.source}) failed: {type(error).__name__}: {error}"
            ) from error
        if not isinstance(text, str):
            raise PreconditionError(
                f"generator {entry.name} ({entry.source}) returned {type(text).__name__}, not the file's text"
            )
        return text if text.endswith("\n") or not text else text + "\n"

    def write(self) -> list[Path]:
        """Write every generated file that differs; returns what changed."""
        written = []
        for path, text in self.outputs().items():
            if not path.is_file() or path.read_text() != text:
                write_atomic(path, text)
                written.append(path)
        return written

    def drifted(self, root: Path) -> list[str]:
        return [
            str(path.relative_to(root))
            for path, text in self.outputs(root).items()
            if not path.is_file() or path.read_text() != text
        ]
