"""nock: a project's toolbox, built by its agents for themselves — an
agent-friendly CLI framework, telemetry of every call and session, and
reflection rounds that turn that telemetry into better tools.

What a project's `.nock/tools/` modules use (`.nock/nock guide` explains it):

    import nock

    @nock.command(cache="src/**", examples=["--path tests"])
    def failures(sh, *, path: str = "tests") -> list[dict]: ...

    @nock.check
    def no_todos(root, changed) -> list[nock.Problem]: ...

    @nock.fixture
    def db(root): ...

    @nock.hook("session-start")
    def remind(payload) -> str: ...

    @nock.generator("src/errors.py")
    def errors(root) -> str: ...
"""

import sys

if sys.version_info < (3, 12):  # noqa: UP036 — vendored, it runs on whatever python3 there is
    sys.exit("nock needs Python 3.12 or newer; set NOCK_PYTHON to one")

from nock.errors import (
    ConflictError,
    Error,
    Exit,
    Failed,
    NotFoundError,
    PreconditionError,
    UsageError,
)
from nock.hooks import Block
from nock.processes import Output
from nock.project import Project
from nock.render import Problem
from nock.tools import check, command, fixture, generator, hook

__all__ = [
    "Block",
    "ConflictError",
    "Error",
    "Exit",
    "Failed",
    "NotFoundError",
    "Output",
    "PreconditionError",
    "Problem",
    "Project",
    "UsageError",
    "check",
    "command",
    "fixture",
    "generator",
    "hook",
]
