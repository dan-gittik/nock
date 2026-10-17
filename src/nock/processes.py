"""Supervised processes: one command run to a bounded conclusion — recorded,
streamed to its log as it goes (so the log survives a SIGKILL), killed on
silence or deadline, and handed back condensed."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nock.condense import clean, excerpt
from nock.errors import ExitCode
from nock.utils import now

if TYPE_CHECKING:
    from nock.project import Project

# Exit codes that say the command could not be run at all, not that it said no.
NOT_RUNNABLE = (126, 127)


@dataclass
class Output:
    """What a supervised process left: its exit code, its output condensed,
    and where the full log is."""

    code: int
    lines: list[str]
    log: Path
    note: str | None = None
    run: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """The condensed output, whole."""
        return "\n".join(self.lines)

    @property
    def raw(self) -> str:
        """The output exactly as the process wrote it."""
        return self.log.read_text(errors="replace")

    def excerpt(self, characters: int) -> str:
        """The condensed output within `characters`, keeping the end, with the
        log's path when anything was left out."""
        text, cut = excerpt(self.lines, characters)
        if cut:
            text += f"\n(full log: {self.log})"
        if self.note:
            text += f"\nnock: {self.note}"
        return text


class Supervisor:
    """Runs one command to a bounded conclusion. Silence, not elapsed time, is
    the hang signal; a heartbeat dot on stdout tells a caller watching the
    output that the child is quiet but alive."""

    def __init__(
        self,
        project: Project,
        command: list[str],
        directory: Path | None = None,
        deadline: float | None = None,
        silence: float | None = None,
        environment: dict[str, str] | None = None,
        echo: bool = False,
        call: int | None = None,
        session: str | None = None,
        stdin: str | None = None,
    ) -> None:
        settings = project.configuration
        self.project = project
        self.command = command
        self.directory = directory or project.here
        self.deadline = deadline if deadline is not None else float(settings.get("run.deadline"))
        silence = silence if silence is not None else float(settings.get("run.silence"))
        self.silence = silence if silence > 0 else float("inf")
        self.environment = environment
        self.echo = echo
        self.call = call
        self.session = session if session is not None else project.current_session()
        self.stdin = stdin
        self.heartbeat = float(settings.get("run.heartbeat"))

    def run(self) -> Output:
        """Spawn, stream, bound and record."""
        store = self.project.store
        number = store.insert(
            "runs",
            {
                "call": self.call,
                "command": json.dumps(self.command),
                "directory": str(self.directory),
                "session": self.session,
                "started": now(),
                "supervisor": os.getpid(),
            },
        )
        log_path = self.project.logs / f"run-{number}.log"
        chunks: list[bytes] = []
        with log_path.open("wb", buffering=0) as log:
            try:
                process = subprocess.Popen(
                    self.command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if self.stdin is not None else subprocess.DEVNULL,
                    cwd=self.directory,
                    env=self.environment,
                    start_new_session=True,
                )
            except (FileNotFoundError, PermissionError) as error:
                note = f"cannot run {self.command[0]}: {error.strerror or error}"
                store.update(
                    "runs",
                    number,
                    {"finished": now(), "exit_code": ExitCode.PRECONDITIONS, "note": note},
                )
                return Output(ExitCode.PRECONDITIONS, [], log_path, note, number)
            store.update("runs", number, {"pid": process.pid})
            if self.stdin is not None and process.stdin is not None:
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.write(self.stdin.encode())
                process.stdin.close()
            try:
                with terminate_as_interrupt():
                    code, note = self.stream(process, log, chunks)
            except KeyboardInterrupt:
                kill_group(process)
                store.update(
                    "runs",
                    number,
                    {"finished": now(), "exit_code": ExitCode.KILLED, "note": "interrupted"},
                )
                raise
        store.update("runs", number, {"finished": now(), "exit_code": code, "note": note})
        text = b"".join(chunks).decode(errors="replace")
        return Output(code, clean(text, self.directory), log_path, note, number)

    def stream(
        self, process: subprocess.Popen[bytes], log: Any, chunks: list[bytes]
    ) -> tuple[int, str | None]:
        assert process.stdout is not None
        started = last_output = last_dot = time.monotonic()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        open_stream = True
        while True:
            moment = time.monotonic()
            if moment - started > self.deadline:
                kill_group(process)
                return ExitCode.TIMED_OUT, f"still running after {self.deadline:g}s; killed"
            if moment - last_output > self.silence:
                kill_group(process)
                return ExitCode.HUNG, f"no output for {self.silence:g}s; killed as hung"
            if (
                self.echo
                and self.heartbeat > 0
                and moment - max(last_output, last_dot) >= self.heartbeat
            ):
                last_dot = moment
                sys.stderr.write(f".[{int(moment - started)}s]")
                sys.stderr.flush()
            if open_stream and selector.select(timeout=0.5):
                chunk = os.read(process.stdout.fileno(), 65536)
                if chunk:
                    log.write(chunk)
                    chunks.append(chunk)
                    last_output = time.monotonic()
                else:
                    selector.unregister(process.stdout)
                    open_stream = False
                continue
            if process.poll() is not None:
                code = process.returncode
                if code < 0:
                    return 128 - code, f"killed by signal {-code}"
                if code in NOT_RUNNABLE:
                    return code, "the command could not be run (not found or not executable)"
                return code, None
            if not open_stream:
                time.sleep(0.05)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def terminate_as_interrupt() -> Iterator[None]:
    """For the duration, SIGTERM stops a run the way Ctrl-C does."""
    try:
        previous = signal.signal(signal.SIGTERM, raise_interrupt)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def raise_interrupt(signal_number: int, frame: object) -> None:
    raise KeyboardInterrupt


def kill_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
