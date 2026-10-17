"""The project: the hub every call starts from — where the repository is, what
it is configured to do, its toolbox and its telemetry."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path

from nock.configuration import Configuration
from nock.errors import PreconditionError
from nock.git import Git, GitError
from nock.store import Store
from nock.tools import Registry

NOCK_DIRECTORY = ".nock"
# Set by nock on the sessions it dispatches; Claude Code sets the session's id.
ROLE_VARIABLE = "NOCK_ROLE"
SESSION_VARIABLE = "CLAUDE_CODE_SESSION_ID"


class Project:
    """One repository under nock. `root` is the main checkout — telemetry lives
    there whichever worktree a call runs in — and `here` is the working tree
    the call runs in, whose `.nock/` code (tools, settings, briefs) it uses."""

    def __init__(self, root: Path, here: Path | None = None) -> None:
        self.root = root
        self.here = here or root

    @classmethod
    def discover(cls, directory: Path | None = None) -> Project:
        directory = (directory or Path.cwd()).resolve()
        try:
            git = Git(directory)
            here = git.toplevel()
            root = git.main_checkout()
        except GitError as error:
            raise PreconditionError(f"{directory} is not inside a git repository") from error
        project = cls(root, here)
        if not (here / NOCK_DIRECTORY).is_dir():
            raise PreconditionError(f"{here} has no {NOCK_DIRECTORY}/ — run `nock init` first")
        return project

    # --- places ------------------------------------------------------------------

    @property
    def local(self) -> Path:
        """Machine state, never committed: telemetry, logs, caches, worktrees."""
        path = self.root / NOCK_DIRECTORY / "local"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs(self) -> Path:
        path = self.local / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def code_path(self, *parts: str) -> Path:
        """A place in the project's nock code — settings, tools, briefs,
        generators, reflections — in the working tree the call runs in."""
        return (self.here / NOCK_DIRECTORY).joinpath(*parts)

    # --- settings, tools and telemetry --------------------------------------------

    @cached_property
    def configuration(self) -> Configuration:
        return Configuration([self.code_path("config.toml"), self.local / "config.toml"])

    @cached_property
    def git(self) -> Git:
        """The main checkout's git."""
        return Git(self.root)

    @property
    def branch(self) -> str:
        return str(self.configuration.get("branch", "main"))

    @cached_property
    def registry(self) -> Registry:
        """Built-ins and the project's `.nock/tools/`, loaded once."""
        return Registry.load(self)

    @cached_property
    def store(self) -> Store:
        return Store(self.local / "nock.db")

    def current_session(self) -> str | None:
        return os.environ.get(SESSION_VARIABLE) or None

    def current_role(self) -> str | None:
        return os.environ.get(ROLE_VARIABLE) or None

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        """An exclusive, cross-process lock named `name`."""
        path = self.local / "locks" / f"{name}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
