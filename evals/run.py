"""Does reflection pay? Builds the scenario project, installs this checkout's
nock into it, runs the training tasks as real Claude Code sessions, runs one
reflection round on what they left, then runs each test task with and without
the toolbox the round built — and compares tokens, cost, turns, time and
success.

    uv run python evals/run.py OUT [--model haiku] [--repeats 2] [--parallel 4]
        [--reflect-model MODEL] [--skip-training]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scenario"))

from build import build  # noqa: E402
from tasks import TEST, TRAINING, Task  # noqa: E402

SOURCE = HERE.parent / "src"
PARENT_SESSION_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
)
SESSION_DEADLINE = 20 * 60
CHECK = "python3 -m unittest discover -s tests -t ."


@dataclass
class Outcome:
    task: str
    kind: str
    arm: str
    repeat: int
    session: str
    passed: bool
    detail: str
    cost: float
    turns: int
    seconds: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    nock_calls: int
    project_tool_calls: int
    final: str


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=directory, check=True, capture_output=True, text=True
    ).stdout.strip()


def environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in PARENT_SESSION_VARIABLES}


def create(project: Path) -> str:
    """The scenario with nock installed and a test check configured; returns the baseline commit."""
    project.mkdir(parents=True)
    build(project)
    git(project, "init", "-q", "-b", "main")
    subprocess.run(
        [sys.executable, "-m", "nock", "init"],
        cwd=project,
        check=True,
        capture_output=True,
        env={**environment(), "PYTHONPATH": str(SOURCE)},
    )
    config = project / ".nock" / "config.toml"
    config.write_text(
        config.read_text().replace(
            '# [checks.test]\n# command = "pytest -q"', f'[checks.test]\ncommand = "{CHECK}"'
        )
    )
    (project / ".gitignore").write_text("__pycache__/\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "The shop")
    return git(project, "rev-parse", "HEAD")


def prepare(directory: Path, base: str, task: Task) -> None:
    git(directory, "reset", "-q", "--hard", base)
    git(directory, "clean", "-qfd", "-e", ".nock/local")
    for file, old, new in task.setup:
        path = directory / file
        text = path.read_text()
        assert old in text, f"{task.name}: {old!r} not in {file}"
        path.write_text(text.replace(old, new, 1))
    if task.setup:
        git(directory, "commit", "-qam", "Tidy up")


def session(directory: Path, task: Task, model: str, arm: str, repeat: int) -> Outcome:
    identity = str(uuid.uuid4())
    command = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        identity,
        "--permission-mode",
        "bypassPermissions",
    ]
    if model:
        command += ["--model", model]
    started = time.monotonic()
    try:
        result = subprocess.run(
            [*command, "--", task.prompt],
            cwd=directory,
            capture_output=True,
            text=True,
            env=environment(),
            timeout=SESSION_DEADLINE,
        )
        stream = result.stdout
    except subprocess.TimeoutExpired as error:
        stream = (
            (error.stdout or b"").decode()
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
    final: dict = {}
    for line in stream.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            final = record
    text = str(final.get("result") or "")
    passed, detail = task.verify(directory, text)
    usage = final.get("usage") or {}
    calls = nock_calls(directory, identity)
    return Outcome(
        task.name, task.kind, arm, repeat, identity, passed, detail,
        float(final.get("total_cost_usd") or 0), int(final.get("num_turns") or 0), round(time.monotonic() - started, 1),
        int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0),
        calls[0], calls[1], text[-500:],
    )  # fmt: skip


def nock_calls(directory: Path, identity: str) -> tuple[int, int]:
    """Calls to nock in the session, and how many of them were the project's own tools."""
    database = directory / ".nock" / "local" / "nock.db"
    if not database.is_file():
        return 0, 0
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT source FROM calls WHERE session = ?", (identity,)
        ).fetchall()
    finally:
        connection.close()
    return len(rows), sum(1 for (source,) in rows if source and source != "nock")


def train(project: Path, base: str, model: str, log: Path) -> None:
    for task in TRAINING:
        prepare(project, base, task)
        outcome = session(project, task, model, "training", 1)
        append(log, outcome)
        print(
            f"training {task.name}: {'pass' if outcome.passed else 'FAIL'} ${outcome.cost:.2f} {outcome.turns} turns",
            flush=True,
        )
    git(project, "reset", "-q", "--hard", base)
    git(project, "clean", "-qfd", "-e", ".nock/local")


def reflect(project: Path, reflect_model: str, out: Path) -> str | None:
    if reflect_model:
        local = project / ".nock" / "local" / "config.toml"
        local.write_text(f'[reflect]\nmodel = "{reflect_model}"\n')
    started = time.monotonic()
    result = subprocess.run(
        [str(project / ".nock" / "nock"), "reflect", "run"],
        cwd=project,
        capture_output=True,
        text=True,
        env=environment(),
    )
    (out / "reflection.txt").write_text(
        f"exit {result.returncode} after {time.monotonic() - started:.0f}s\n\n{result.stdout}\n{result.stderr}"
    )
    print(
        f"reflection: exit {result.returncode}\n{result.stdout[-2000:]}{result.stderr[-1000:]}",
        flush=True,
    )
    if result.returncode != 0:
        return None
    return git(project, "rev-parse", "HEAD")


def append(log: Path, outcome: Outcome) -> None:
    with log.open("a") as handle:
        handle.write(json.dumps(asdict(outcome)) + "\n")


def test(
    project: Path,
    out: Path,
    arms: dict[str, str],
    model: str,
    repeats: int,
    parallel: int,
    log: Path,
) -> None:
    jobs = [
        (task, arm, repeat) for repeat in range(1, repeats + 1) for task in TEST for arm in arms
    ]

    def run(job: tuple[Task, str, int]) -> Outcome:
        task, arm, repeat = job
        directory = out / "runs" / f"{task.name}-{arm}-{repeat}"
        shutil.rmtree(directory, ignore_errors=True)
        subprocess.run(["git", "clone", "-q", str(project), str(directory)], check=True)
        prepare(directory, arms[arm], task)
        outcome = session(directory, task, model, arm, repeat)
        append(log, outcome)
        print(
            f"{arm} {task.name} #{repeat}: {'pass' if outcome.passed else 'FAIL'} ${outcome.cost:.3f} {outcome.turns} turns {outcome.seconds:.0f}s, {outcome.project_tool_calls} tool calls",
            flush=True,
        )
        return outcome

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        list(pool.map(run, jobs))


def report(log: Path, out: Path) -> str:
    outcomes = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    tested = [outcome for outcome in outcomes if outcome["arm"] != "training"]
    lines = ["# nock eval", ""]
    metrics = ("passed", "cost", "turns", "seconds", "tokens", "nock_calls", "project_tool_calls")
    for outcome in tested:
        outcome["tokens"] = sum(
            outcome[key]
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        )
    lines.append("| kind | arm | runs | " + " | ".join(metrics) + " |")
    lines.append("|---" * (len(metrics) + 3) + "|")
    kinds = [*sorted({outcome["kind"] for outcome in tested}), "all"]
    for kind in kinds:
        for arm in ("control", "toolbox"):
            group = [
                outcome
                for outcome in tested
                if outcome["arm"] == arm and kind in (outcome["kind"], "all")
            ]
            if not group:
                continue
            cells = []
            for metric in metrics:
                values = [float(outcome[metric]) for outcome in group]
                mean = statistics.mean(values)
                cells.append(
                    f"{mean:.0%}"
                    if metric == "passed"
                    else f"{mean:,.3f}"
                    if metric == "cost"
                    else f"{mean:,.0f}"
                    if metric == "tokens"
                    else f"{mean:.1f}"
                )
            lines.append(f"| {kind} | {arm} | {len(group)} | " + " | ".join(cells) + " |")
    lines += ["", "Per task (control → toolbox, means):", ""]
    for task in TEST:
        pair = {
            arm: [
                outcome
                for outcome in tested
                if outcome["task"] == task.name and outcome["arm"] == arm
            ]
            for arm in ("control", "toolbox")
        }
        if not all(pair.values()):
            continue
        cost = {
            arm: statistics.mean(outcome["cost"] for outcome in group)
            for arm, group in pair.items()
        }
        turns = {
            arm: statistics.mean(outcome["turns"] for outcome in group)
            for arm, group in pair.items()
        }
        passed = {arm: sum(outcome["passed"] for outcome in group) for arm, group in pair.items()}
        lines.append(
            f"- {task.name}: ${cost['control']:.3f} → ${cost['toolbox']:.3f}, {turns['control']:.1f} → {turns['toolbox']:.1f} turns, "
            f"passed {passed['control']}/{len(pair['control'])} → {passed['toolbox']}/{len(pair['toolbox'])}"
        )
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("out", type=Path)
    parser.add_argument("--model", default="haiku", help="The model the task sessions run on.")
    parser.add_argument(
        "--reflect-model",
        default="",
        help="The reflecting session's model (default: Claude Code's).",
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument(
        "--skip-training", action="store_true", help="Reuse OUT/project and its reflection."
    )
    parser.add_argument("--report-only", action="store_true")
    arguments = parser.parse_args()
    out: Path = arguments.out.resolve()
    log = out / "outcomes.jsonl"
    if arguments.report_only:
        print(report(log, out))
        return
    project = out / "project"
    if not arguments.skip_training:
        if out.exists():
            sys.exit(f"{out} exists; pick a new directory, or --skip-training to reuse it")
        out.mkdir(parents=True)
        base = create(project)
        (out / "base").write_text(base)
        train(project, base, arguments.model, log)
        toolbox = reflect(project, arguments.reflect_model, out)
        if toolbox is None:
            sys.exit("the reflection round did not land; see reflection.txt")
        (out / "toolbox").write_text(toolbox)
    arms = {
        "control": (out / "base").read_text().strip(),
        "toolbox": (out / "toolbox").read_text().strip(),
    }
    test(project, out, arms, arguments.model, arguments.repeats, arguments.parallel, log)
    print(report(log, out))


if __name__ == "__main__":
    main()
