"""The exit-code taxonomy and the error classes that carry it: an anticipated
failure is a specific class whose code says whether to change the request, fix
the code, or fix the machine."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, ClassVar


class ExitCode(IntEnum):
    """Shaped after HTTP: 4x means change what you asked, 5x means retry or
    escalate, and 1 sits outside both — a failing check is not an error but an
    evaluation whose answer is no. Everything stays below 126, which shells
    claim."""

    SUCCESS = 0
    SUBJECT_FAILED = 1

    USAGE = 40
    AUTHENTICATION = 41
    PRECONDITIONS = 42
    POLICY = 43
    NOT_FOUND = 44
    CONFLICT = 49

    INTERNAL = 50
    EXTERNAL = 52
    TIMED_OUT = 54
    HUNG = 55
    KILLED = 56


class Error(Exception):
    """An anticipated failure: a message and the exit code that classifies it.
    Unanticipated failures are not wrapped — they crash loudly."""

    exit_code: ClassVar[ExitCode] = ExitCode.INTERNAL

    def __init__(self, message: str = "", value: Any = None) -> None:
        super().__init__(message)
        # Evidence printed like a return value: `raise nock.Failed("2 broken", rows)`.
        self.value = value

    @classmethod
    def for_exit_code(cls, exit_code: int, message: str, value: Any = None) -> Error:
        """The specific error class carrying a code decided at runtime."""
        for subclass in Error.__subclasses__():
            if subclass.exit_code == exit_code:
                return subclass(message, value)
        return InternalError(message, value)


class SubjectFailedError(Error):
    """The evaluation ran and its answer is no."""

    exit_code = ExitCode.SUBJECT_FAILED


Failed = SubjectFailedError


class UsageError(Error):
    exit_code = ExitCode.USAGE


class AuthenticationError(Error):
    exit_code = ExitCode.AUTHENTICATION


class PreconditionError(Error):
    """The machine or the state is not ready — fix that first, never the code."""

    exit_code = ExitCode.PRECONDITIONS


class PolicyError(Error):
    exit_code = ExitCode.POLICY


class NotFoundError(Error):
    exit_code = ExitCode.NOT_FOUND


class ConflictError(Error):
    exit_code = ExitCode.CONFLICT


class InternalError(Error):
    exit_code = ExitCode.INTERNAL


class ExternalError(Error):
    exit_code = ExitCode.EXTERNAL


class TimedOutError(Error):
    """Still working when the clock ran out — raise the bound or split the work."""

    exit_code = ExitCode.TIMED_OUT


class HungError(Error):
    """Stopped saying anything — suspect a deadlock."""

    exit_code = ExitCode.HUNG


class KilledError(Error):
    """Killed by nock — distinct from 137, an external SIGKILL."""

    exit_code = ExitCode.KILLED


class Exit(Exception):
    """End the call with this exit code and nothing more to say: `nock run`
    passes on its child's code this way."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code
