"""The project's settings: nock's defaults, overlaid by `.nock/config.toml`,
overlaid by the machine's own `.nock/local/config.toml`."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from nock.errors import PreconditionError

DEFAULTS: dict[str, Any] = {
    # The branch reflection lands on.
    "branch": "main",
    "output": {
        # The most characters a call prints before the rest goes to a file. Claude
        # Code shows a failed command only ~10k characters, middle cut, no path.
        "characters": 8000,
        # The most rows of a table a call prints before the rest goes to a file.
        "rows": 60,
    },
    "run": {
        # Bounds on a supervised process (`sh`, `nock run`), in seconds; silence
        # is how long it may print nothing before it counts as hung.
        "deadline": 3600,
        "silence": 900,
        # A dot on stdout per this many seconds of silence, so a watcher knows
        # the process is alive.
        "heartbeat": 60,
    },
    "checks": {
        # How many check suites may run at once on this machine.
        "concurrency": 2,
    },
    "reflect": {
        # A round is due after this many sessions since the last one, or after
        # `days` days with at least `minimum` sessions.
        "sessions": 15,
        "days": 7,
        "minimum": 3,
        # The headless agent and its model ("" is Claude Code's default).
        "command": ["claude"],
        "model": "",
        "deadline": 2 * 3600,
        "silence": 1800,
    },
    # Dollars per million tokens, by model-name prefix: {input, output, cache_read, cache_write}.
    "pricing": {},
}


def merged(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


class Configuration:
    """Settings read by dotted key: `configuration.get("run.deadline")`."""

    def __init__(self, files: list[Path]) -> None:
        self.values = DEFAULTS
        for file in files:
            if file.is_file():
                try:
                    self.values = merged(self.values, tomllib.loads(file.read_text()))
                except tomllib.TOMLDecodeError as error:
                    raise PreconditionError(f"{file} is not valid TOML: {error}") from error

    def get(self, key: str, default: Any = None) -> Any:
        value: Any = self.values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def section(self, key: str) -> dict[str, Any]:
        value = self.get(key, {})
        return value if isinstance(value, dict) else {}
