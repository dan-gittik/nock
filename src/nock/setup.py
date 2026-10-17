"""Installing nock into a project — vendored, so it becomes the project's own
code — and upgrading the vendored core with a three-way merge, so local edits
survive."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from nock.errors import ConflictError, PreconditionError
from nock.git import Git, GitError
from nock.hooks import BUILT_IN_EVENTS
from nock.project import NOCK_DIRECTORY, Project
from nock.tools import NATIVE_EVENTS
from nock.utils import read_json, write_atomic, write_json

PACKAGE = Path(__file__).resolve().parent
VERSION = "0.2.0"
DEFAULT_SOURCE = "https://github.com/dan-gittik/nock"

SHIM = """\
#!/bin/sh
# nock, vendored: the code is in .nock/core and belongs to this project.
# It needs Python 3.12 or newer and nothing else; NOCK_PYTHON picks the interpreter.
here="$(cd "$(dirname "$0")" && pwd -P)"
PYTHONPATH="$here/core${PYTHONPATH:+:$PYTHONPATH}" exec "${NOCK_PYTHON:-python3}" -m nock "$@"
"""

CONFIG = """\
# nock's settings. Defaults live in .nock/core/nock/configuration.py;
# machine-specific overrides go in .nock/local/config.toml (not committed).

branch = "main"

# Checks `.nock/nock check` runs besides the @nock.check functions in
# .nock/tools/. Exit 0 passes, 1 fails (the code is wrong), anything else
# means the machine is.
# [checks.lint]
# command = "ruff check ."
# fix = "ruff check --fix ."
#
# [checks.test]
# command = "pytest -q"

[reflect]
# A round is due after this many sessions, or `days` days with `minimum` sessions.
sessions = 15
days = 7
minimum = 3
# The reflecting agent's model; empty is Claude Code's default.
model = ""
"""

GITIGNORE = "local/\n"

PRIMER = """\
## nock

`.nock/nock` is this project's toolbox: a CLI whose commands are the
project's own code, in `.nock/tools/`, written by agents for agents.
`.nock/nock` lists them, and every session starts with the project's tools
listed. Reach for a tool before doing the same work by hand.

- A call prints only what the next step needs — nothing when the exit code
  says it all. Exit codes: 0 yes; 1 no, the subject failed (fix the code);
  40–49 change the request (42: fix the machine or the state, not the code);
  50–56 retry or escalate. `--json` gives JSON, `--full` uncapped output; long
  output is cut, and the rest saved to a file whose path is printed.
- `.nock/nock check` runs the project's checks. `.nock/nock run -- COMMAND`
  runs anything noisy or long, supervised, with its output condensed.
- Every call and every session is recorded. Reflection rounds turn that
  record into better tools: when a session starts by saying a round is due,
  run `.nock/nock reflect run` in the background.
- When a chore repeats, make it a tool — `.nock/nock guide` says how, in a
  page; it is live on the next call. When a rule can be checked mechanically,
  make it a check, not a note: a check beats a hook beats prose.
"""

IMPORT_LINE = "@.nock/context.md"
HOOK_COMMAND = '"$CLAUDE_PROJECT_DIR"/.nock/nock hook {event}'
PERMISSION = "Bash(.nock/nock:*)"


def wired_events(root: Path) -> set[str]:
    """The Claude Code events this checkout's settings send to nock."""
    settings_path = root / ".claude" / "settings.json"
    if not settings_path.is_file():
        return set()
    try:
        hooks = read_json(settings_path).get("hooks", {})
    except PreconditionError:
        return set()
    return {
        event
        for event, entries in hooks.items()
        if any(
            ".nock/nock hook" in str(hook.get("command", ""))
            for entry in entries
            if isinstance(entry, dict)
            for hook in entry.get("hooks", [])
        )
    }


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        lines = []
        if self.written:
            lines.append("wrote: " + ", ".join(self.written))
        if self.kept:
            lines.append("kept (already there): " + ", ".join(self.kept))
        if self.conflicts:
            lines.append("CONFLICTS to resolve by hand: " + ", ".join(self.conflicts))
        return lines


def core_files(root: Path) -> dict[str, bytes]:
    """The core's files under `root`, by relative path, caches left out."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


class Installer:
    def __init__(self, target: Path) -> None:
        try:
            self.root = Git(target).main_checkout()
        except GitError as error:
            raise PreconditionError(
                f"{target} is not a git repository — `git init` it first"
            ) from error
        self.directory = self.root / NOCK_DIRECTORY
        self.report = Report()

    def install(self, source: str | None = None) -> Report:
        source = source or DEFAULT_SOURCE
        core = self.directory / "core" / "nock"
        if core.resolve() != PACKAGE:
            if core.exists():
                raise ConflictError(f"{core} exists — use `nock upgrade` to update a vendored core")
            self.copy_core(core)
            write_json(
                self.directory / "core" / "VERSION.json",
                {"version": VERSION, "source": source, "commit": self.source_commit()},
            )
        self.write("nock", SHIM, executable=True, replace=True)
        self.write(".gitignore", GITIGNORE, replace=False)
        self.write("config.toml", CONFIG, replace=False)
        self.write("context.md", PRIMER, replace=True)
        self.write("tools/.gitkeep", "", replace=False)
        self.import_context()
        self.wire_claude()
        return self.report

    def copy_core(self, core: Path) -> None:
        for relative, content in core_files(PACKAGE).items():
            path = core / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.report.written.append(f"{NOCK_DIRECTORY}/core/")

    def source_commit(self) -> str | None:
        """The upstream commit this core was copied from, when it came from a checkout."""
        git = Git(PACKAGE)
        if not git.succeeds("ls-files", "--error-unmatch", "__init__.py") or git.dirty("."):
            return None
        return git.head()

    def write(
        self, relative: str, text: str, executable: bool = False, replace: bool = False
    ) -> None:
        path = self.directory / relative
        if path.exists() and not replace:
            self.report.kept.append(f"{NOCK_DIRECTORY}/{relative}")
            return
        write_atomic(path, text)
        if executable:
            path.chmod(0o755)
        self.report.written.append(f"{NOCK_DIRECTORY}/{relative}")

    def import_context(self) -> None:
        """CLAUDE.md imports `.nock/context.md`, so every session reads the
        primer without nock ever editing CLAUDE.md again."""
        claude = self.root / "CLAUDE.md"
        text = claude.read_text() if claude.is_file() else ""
        if IMPORT_LINE in text.splitlines():
            return
        claude.write_text(text.rstrip("\n") + ("\n\n" if text.strip() else "") + IMPORT_LINE + "\n")
        self.report.written.append("CLAUDE.md")

    def wire_claude(self) -> None:
        settings_path = self.root / ".claude" / "settings.json"
        settings = read_json(settings_path) if settings_path.is_file() else {}
        hooks = settings.setdefault("hooks", {})
        listened = {*BUILT_IN_EVENTS, *(entry.event for entry in Project(self.root).registry.hooks)}
        for event in sorted(NATIVE_EVENTS[event] for event in listened):
            command = HOOK_COMMAND.format(event=event)
            entries = hooks.setdefault(event, [])
            entries[:] = [
                entry
                for entry in entries
                if not any(
                    ".nock/nock hook" in str(hook.get("command", ""))
                    for hook in entry.get("hooks", [])
                )
            ]
            entries.append({"hooks": [{"type": "command", "command": command}]})
        allowed = settings.setdefault("permissions", {}).setdefault("allow", [])
        if PERMISSION not in allowed:
            allowed.append(PERMISSION)
        write_json(settings_path, settings)
        self.report.written.append(".claude/settings.json")


class Upgrader:
    """Three-way merge of the vendored core: the version it was vendored from
    (base), the project's copy (ours), and upstream (theirs)."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.core = project.root / NOCK_DIRECTORY / "core" / "nock"
        self.version_file = project.root / NOCK_DIRECTORY / "core" / "VERSION.json"

    def upgrade(self, source: str | None = None, force: bool = False) -> Report:
        version = read_json(self.version_file) if self.version_file.is_file() else {}
        source = str(source or version.get("source") or DEFAULT_SOURCE)
        report = Report()
        with tempfile.TemporaryDirectory() as scratch:
            upstream = self.fetch(source, Path(scratch))
            theirs = core_files(upstream / "src" / "nock")
            base = self.base(upstream, version.get("commit"))
            if base is None and not force:
                raise PreconditionError(
                    "cannot find the version this core was vendored from upstream, so local edits "
                    "cannot be merged safely — `--force` replaces the core, losing them"
                )
            ours = core_files(self.core)
            names: set[str] = {*theirs, *ours, *(base or {})}
            for relative in sorted(names):
                merged = self.merge(
                    relative,
                    ours.get(relative),
                    (base or {}).get(relative),
                    theirs.get(relative),
                    force,
                )
                path = self.core / relative
                if merged is None:
                    if path.exists():
                        path.unlink()
                        report.written.append(relative)
                    continue
                content, conflicted = merged
                if ours.get(relative) != content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    report.written.append(relative)
                if conflicted:
                    report.conflicts.append(relative)
            commit = Git(upstream).head() if Git(upstream).succeeds("rev-parse", "HEAD") else None
            upstream_version = self.upstream_version(upstream)
        write_json(
            self.version_file, {"version": upstream_version, "source": source, "commit": commit}
        )
        return report

    def fetch(self, source: str, scratch: Path) -> Path:
        if Path(source).is_dir():
            return Path(source)
        result = subprocess.run(
            ["git", "clone", "--quiet", source, str(scratch / "upstream")],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PreconditionError(f"cannot clone {source}: {result.stderr.strip()}")
        return scratch / "upstream"

    def base(self, upstream: Path, commit: str | None) -> dict[str, bytes] | None:
        if not commit:
            return None
        git = Git(upstream)
        if not git.succeeds("cat-file", "-e", f"{commit}^{{commit}}"):
            return None
        names = git("ls-tree", "-r", "--name-only", commit, "src/nock").splitlines()
        return {
            name.removeprefix("src/nock/"): subprocess.run(
                ["git", "show", f"{commit}:{name}"], cwd=upstream, capture_output=True, check=True
            ).stdout
            for name in names
        }

    def merge(
        self,
        relative: str,
        ours: bytes | None,
        base: bytes | None,
        theirs: bytes | None,
        force: bool,
    ) -> tuple[bytes, bool] | None:
        """The file's merged content and whether it conflicted, or None to delete it."""
        if force:
            return (theirs, False) if theirs is not None else None
        if ours == base:
            return (theirs, False) if theirs is not None else None
        if theirs in (base, ours):
            return (ours, False) if ours is not None else None
        if ours is None or theirs is None:
            return ((ours or theirs or b""), True)
        with tempfile.TemporaryDirectory() as scratch:
            paths = []
            for name, content in (("ours", ours), ("base", base or b""), ("theirs", theirs)):
                path = Path(scratch) / name
                path.write_bytes(content)
                paths.append(str(path))
            result = subprocess.run(
                [
                    "git",
                    "merge-file",
                    "-p",
                    "-L",
                    "project",
                    "-L",
                    "vendored",
                    "-L",
                    "upstream",
                    *paths,
                ],
                capture_output=True,
            )
        return result.stdout, result.returncode != 0

    def upstream_version(self, upstream: Path) -> str:
        pyproject = upstream / "pyproject.toml"
        if pyproject.is_file():
            for line in pyproject.read_text().splitlines():
                if line.startswith("version"):
                    return json.loads(line.split("=", 1)[1].strip())
        return "unknown"
