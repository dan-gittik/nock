"""Sessions read into telemetry, the hooks around them, and reflection rounds:
the material a round reads, and a round that lands — or does not."""

from __future__ import annotations

import json
import os
from pathlib import Path

from nock.hooks import HookRunner
from nock.project import Project
from nock.reflect import Reflection, Rounds, shape
from nock.sessions import Sessions, encoded

from .conftest import git, nock, write_tool


def transcript(
    project: Project, session: str, prompts: list[str], commands: list[tuple[str, int, bool]]
) -> Path:
    """A Claude Code transcript: the human's prompts, then Bash calls — each a
    command, how many bytes it printed and whether it failed — a second apart."""
    directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / encoded(project.root)
    directory.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    second = 0

    def moment() -> str:
        return f"2026-10-01T10:00:{second:02d}.000Z"

    for prompt in prompts:
        records.append(
            {
                "type": "user",
                "timestamp": moment(),
                "cwd": str(project.root),
                "message": {"role": "user", "content": prompt},
            }
        )
    for index, (command, size, failed) in enumerate(commands):
        second += 1
        records.append(
            {
                "type": "assistant",
                "timestamp": moment(),
                "message": {
                    "id": f"m{index}",
                    "model": "claude-test",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 1000,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{index}",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                },
            }
        )
        second += 1
        records.append(
            {
                "type": "user",
                "timestamp": moment(),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{index}",
                            "content": "x" * size,
                            "is_error": failed,
                        }
                    ],
                },
            }
        )
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def test_a_transcript_becomes_prompts_events_and_cost(project: Project) -> None:
    path = transcript(
        project,
        "s1",
        ["Fix the login bug", "no, the other login"],
        [("pytest -q tests/test_login.py | tail -20", 5000, True), (".nock/nock check", 0, False)],
    )
    Sessions(project).ingest(path)
    session = project.store.rows("SELECT * FROM sessions")[0]
    assert session["title"] == "Fix the login bug" and session["prompts"] == 2
    assert session["input_tokens"] == 200 and session["cache_read_tokens"] == 2000
    events = project.store.rows("SELECT tool, input, failed, output_bytes, duration FROM events")
    assert events[0] == {
        "tool": "Bash",
        "input": "pytest -q tests/test_login.py | tail -20",
        "failed": 1,
        "output_bytes": 5000,
        "duration": 1.0,
    }
    prompts = [row["text"] for row in project.store.rows("SELECT text FROM prompts")]
    assert prompts == ["Fix the login bug", "no, the other login"]
    Sessions(project).ingest(path)
    assert len(project.store.rows("SELECT * FROM events")) == 2


def test_the_session_hooks_list_the_tools_and_read_the_transcript(project: Project) -> None:
    write_tool(
        project,
        "work",
        "import nock\n\n@nock.command\ndef deploy(*, target: str = 'staging'):\n    '''Ship it. Carefully.'''\n",
    )
    start = HookRunner(project, {"session_id": "s2"}).handle("SessionStart")
    assert start is not None
    context = start["hookSpecificOutput"]["additionalContext"]
    assert "- deploy [--target TARGET]: Ship it" in context
    path = transcript(project, "s2", ["Deploy it"], [("make deploy", 10, False)])
    assert (
        HookRunner(project, {"session_id": "s2", "transcript_path": str(path)}).handle("SessionEnd")
        is None
    )
    assert project.store.rows("SELECT id FROM sessions") == [{"id": "s2"}]


def test_a_round_is_due_after_enough_sessions(project: Project) -> None:
    local = project.root / ".nock" / "local" / "config.toml"
    local.write_text(
        local.read_text().replace("[reflect]\n", "[reflect]\nsessions = 4\ndays = 30\n")
    )
    for index in range(3):
        transcript(project, f"s{index}", ["work"], [])
    Sessions(project).catch_up()
    assert Reflection(project).due() == ""
    transcript(project, "s3", ["more work"], [])
    Sessions(project).catch_up()
    assert Reflection(project).due() == "a reflection round is due (4 sessions since the last)"


def test_the_material_starts_from_the_work_and_names_the_waste(project: Project) -> None:
    write_tool(
        project, "work", "import nock\n\n@nock.command\ndef unused():\n    '''Never called.'''\n"
    )
    git(project.root, "commit", "--allow-empty", "-qm", "Add login rate limiting")
    for index in range(3):
        transcript(
            project,
            f"session-{index}",
            ["Why does login fail?", "no — run the slow suite too"],
            [
                ("cd app && pytest -q tests/ | tail -30", 9000, True),
                ("grep -rn TODO src", 300, False),
                ("pytest -q tests/", 9000, True),
            ],
        )
    nock(project.root, "unusd")
    for _ in range(12):
        nock(project.root, "calls", "--limit", "1")
    material = Reflection(project).material()
    assert material.index("# What the human asked for") < material.index("# The toolbox")
    assert "- no — run the slow suite too" in material
    assert "Add login rate limiting" in material
    assert "pytest,6,3,6,3," in material
    assert "pytest → grep ×3" in material
    assert "candidates to retire or merge: unused" in material
    assert "`unusd` ×1" in material
    assert "This is the first round." in material


def test_shapes_name_what_a_command_is() -> None:
    assert shape("cd src && FOO=1 python -m pytest -x tests") == "python -m pytest"
    assert shape("git log --oneline | head") == "git log"
    assert shape("ls -la") == "ls"


def test_a_round_lands_its_tool_and_its_record(project: Project) -> None:
    result = nock(project.root, "reflect", "run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "landed:" in result.stdout and "greet" in result.stdout
    assert (project.root / ".nock" / "tools" / "greet.py").is_file()
    assert nock(project.root, "greet", "--name", "dan").stdout == "hello dan\n"
    rounds = Rounds(project).all()
    assert (
        len(rounds) == 1 and rounds[0]["changes"][0]["expect"] == "greet is called at least twice"
    )
    assert "snapshot" in rounds[0]
    assert "Reflection" in git(project.root, "log", "-1", "--format=%s")
    assert git(project.root, "branch", "--list", "nock/reflect-*") == ""
    material = Reflection(project).material()
    assert "# The last round" in material and "expected greet is called at least twice" in material


def test_a_round_without_a_record_or_with_a_broken_tool_does_not_land(
    project: Project, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_REFLECT", "silent")
    silent = nock(project.root, "reflect", "run")
    assert silent.returncode == 1 and "left no record" in silent.stdout
    monkeypatch.setenv("FAKE_REFLECT", "broken")
    broken = nock(project.root, "reflect", "run")
    assert broken.returncode == 1 and "checks fail on the round's branch" in broken.stdout
    assert "the greeting machine is down" in broken.stdout
    assert not (project.root / ".nock" / "tools" / "greet.py").exists()
    assert git(project.root, "branch", "--list", "nock/reflect-*") != ""
