"""A stand-in for `claude -p` in the reflector's seat: a real process that does
what a scripted round would — through nock's own CLI, in the worktree it was
started in — and writes a transcript where Claude Code would. FAKE_REFLECT
picks the round: `add` (a tool and its record), `silent` (no record), `broken`
(a tool whose example fails)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

TOOL = '''\
import nock


@nock.command(examples=["--name x"])
def greet(*, name: str = "world") -> str:
    """Say hello."""
    return f"hello {name}"
'''

BROKEN = '''\
import nock


@nock.command(examples=[""])
def greet() -> str:
    """Say hello, badly."""
    raise nock.PreconditionError("the greeting machine is down")
'''


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(arguments), capture_output=True, text=True)


def write_transcript(session: str, prompt: str) -> None:
    root = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    )
    root.mkdir(parents=True, exist_ok=True)
    moment = datetime.now(UTC).isoformat()
    records = [
        {
            "type": "user",
            "timestamp": moment,
            "cwd": os.getcwd(),
            "message": {"role": "user", "content": prompt[:200]},
        },
        {
            "type": "assistant",
            "timestamp": moment,
            "message": {
                "id": "m1",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [],
            },
        },
    ]
    (root / f"{session}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )


def main() -> int:
    arguments = sys.argv[1:]
    session = arguments[arguments.index("--session-id") + 1]
    prompt = arguments[-1]
    behavior = os.environ.get("FAKE_REFLECT", "add")
    assert "# The material" in prompt and os.environ.get("NOCK_ROLE") == "reflector"
    if behavior in ("add", "broken"):
        Path(".nock/tools/greet.py").write_text(TOOL if behavior == "add" else BROKEN)
        run("git", "add", "-A")
        run("git", "commit", "-qm", "Add greet: the human keeps asking for greetings")
        record = {
            "summary": "Added greet.",
            "changes": [
                {
                    "kind": "add",
                    "target": "greet",
                    "why": "asked for 3 times",
                    "expect": "greet is called at least twice",
                }
            ],
        }
        result = run(".nock/nock", "reflect", "record", json.dumps(record))
        print(json.dumps({"type": "system", "message": result.stdout + result.stderr}), flush=True)
    write_transcript(session, prompt)
    print(json.dumps({"type": "result", "result": f"round done ({behavior})"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
