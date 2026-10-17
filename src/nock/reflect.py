"""Reflection: a round in which an agent reads what the work has been — the
human's requests, the sessions, the toolbox's telemetry, where the code is
going — and changes the toolbox: builds tools for what keeps costing turns and
tokens, repairs and retunes the ones that misbehave, retires the ones nobody
needs. It works on a branch in a worktree of its own, writes down what it
expects of each change, and lands only when every check passes. The next round
holds those expectations against what actually happened."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from nock.commands import schema_of
from nock.dispatch import Dispatcher
from nock.errors import ConflictError, PreconditionError
from nock.git import Git, GitError
from nock.schema import validate
from nock.sessions import Sessions
from nock.templates import Library
from nock.utils import format_duration, now, parse_moment, read_json, write_json

if TYPE_CHECKING:
    from nock.project import Project

ROLE = "reflector"
ROUND_VARIABLE = "NOCK_ROUND"
BRIEFS = Path(__file__).resolve().parent / "briefs"
# Programs whose second word says what they do: `git status`, `uv run`.
TWO_WORDS = {
    "git", "uv", "npm", "npx", "pnpm", "yarn", "poetry", "cargo", "go", "make", "docker",
    "kubectl", "gh", "pip", "bundle", "rails", "dotnet", "mvn", "gradle", "terraform", "brew",
}  # fmt: skip
# Where an agent cut a command's output down by hand.
TRIMMERS = re.compile(r"\|\s*(head|tail|grep|sed -n|awk|cut|wc)\b")
SECTION_ROWS = 25


class Change(TypedDict):
    kind: Literal["add", "change", "retire", "check", "generator", "other"]
    target: str
    """The tool, check or file: `failures`, `.nock/tools/db.py`."""
    why: str
    """The evidence: what in the material called for it."""
    expect: str
    """What should be measurably different by the next round, and how to tell."""


class Verdict(TypedDict):
    expectation: str
    held: bool | None
    evidence: str


class Record(TypedDict):
    summary: str
    changes: list[Change]
    verdicts: NotRequired[list[Verdict]]
    notes: NotRequired[str]


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")


def shape(command: str) -> str:
    """What a shell command is, in a word or three: `pytest`, `git log`,
    `python -m pytest`."""
    first = command.strip().split("\n")[0]
    segments = [segment.strip() for segment in re.split(r"&&|;|\|\|", first)]
    segment = next((part for part in segments if part and not part.startswith("cd ")), first)
    segment = segment.split("|")[0]
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    words = [word for word in words if not re.match(r"^[A-Z_][A-Z0-9_]*=", word)]
    if not words:
        return first[:40]
    program = os.path.basename(words[0])
    if program.startswith("python") and len(words) > 2 and words[1] == "-m":
        return f"{program} -m {words[2]}"
    if program in TWO_WORDS and len(words) > 1 and not words[1].startswith("-"):
        return f"{program} {words[1]}"
    return program


def is_nock(command: str) -> bool:
    return ".nock/nock" in command


def kilobytes(count: float | None) -> str:
    if count is None:
        return "-"
    return f"{count / 1000:.1f}k" if count >= 1000 else str(int(count))


def seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}s" if value < 60 else format_duration(value)


@dataclass
class Window:
    since: str | None
    sessions: list[dict[str, Any]]


class Rounds:
    """The record of every round: `.nock/reflections/STAMP.json`, committed, one
    file per round so that rounds on different machines merge."""

    def __init__(self, project: Project) -> None:
        self.project = project

    @property
    def directory(self) -> Path:
        return self.project.code_path("reflections")

    def all(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        records = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = read_json(path)
            except PreconditionError:
                continue
            record.setdefault("round", path.stem)
            records.append(record)
        return sorted(records, key=lambda record: record.get("finished") or record["round"])

    def last(self) -> dict[str, Any] | None:
        records = self.all()
        return records[-1] if records else None

    def write(self, name: str, record: Record) -> Path:
        validate(record, schema())
        path = self.directory / f"{name}.json"
        existing = read_json(path) if path.is_file() else {}
        write_json(path, {**existing, **record, "round": name, "finished": now()})
        return path


def schema() -> dict[str, Any]:
    return schema_of(Record)


class Reflection:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.rounds = Rounds(project)

    # --- when -----------------------------------------------------------------

    def window(self) -> Window:
        last = self.rounds.last()
        since = last.get("finished") if last else None
        return Window(since, Sessions(self.project).since(since))

    def due(self) -> str:
        """Why a round is due, or nothing when it is not."""
        settings = self.project.configuration
        window = self.window()
        count = len(window.sessions)
        if count >= int(settings.get("reflect.sessions")):
            return f"a reflection round is due ({count} sessions since the last)"
        since = parse_moment(window.since) or (
            parse_moment(window.sessions[0]["started"]) if window.sessions else None
        )
        days = (datetime.now(UTC) - since).total_seconds() / 86400 if since else 0
        if days >= float(settings.get("reflect.days")) and count >= int(
            settings.get("reflect.minimum")
        ):
            return f"a reflection round is due ({count} sessions over {days:.0f} days)"
        return ""

    # --- the material -----------------------------------------------------------

    def material(self) -> str:
        Sessions(self.project).catch_up()
        return Material(self.project, self.window(), self.rounds.last()).render()

    def snapshot(self, since: str | None) -> dict[str, Any]:
        """Each tool's numbers over the window, kept in the round's record so
        the next round can compare."""
        return {
            row["tool"]: {
                key: row[key]
                for key in ("calls", "failures", "sessions", "median_seconds", "mean_bytes")
            }
            for row in tool_rows(self.project, since)
        }

    # --- the round ----------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """One round, start to finish: material, a dispatched session on a
        branch, then — when it left a record and every check passes — landing."""
        with self.project.lock("reflect"):
            name = self.unused_name()
            since = self.window().since
            material = self.material()
            report = self.project.local / "reflections" / f"{name}.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(material)
            worktree = Worktree(self.project, name)
            path = worktree.create()
            brief = Library(self.project.code_path("briefs"), BRIEFS).render(
                "reflect.md",
                material=material,
                directory=str(path),
                round=name,
                rounds=len(self.rounds.all()),
            )
            session = Dispatcher(self.project).launch(ROLE, brief, path, {ROUND_VARIABLE: name})
            outcome: dict[str, Any] = {"round": name, "session": session.id, "worktree": str(path)}
            if session.exit_code != 0:
                return {
                    **outcome,
                    "landed": None,
                    "problem": f"the session ended with exit {session.exit_code} (log: {session.log})",
                }
            return {**outcome, **self.land(worktree, name, since, session.result)}

    def unused_name(self) -> str:
        """The round's name: when it started, made unique against rounds kept for inspection."""
        name, count = stamp(), 1
        while self.project.git.succeeds(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{Worktree(self.project, name).branch}"
        ):
            count += 1
            name = f"{stamp()}-{count}"
        return name

    def land(
        self, worktree: Worktree, name: str, since: str | None, last_word: str | None
    ) -> dict[str, Any]:
        git = worktree.git
        record_path = worktree.path / ".nock" / "reflections" / f"{name}.json"
        if not record_path.is_file():
            return {
                "landed": None,
                "problem": f"the round left no record — the session said: {(last_word or '')[:400]}",
            }
        record = read_json(record_path)
        record["snapshot"] = self.snapshot(since)
        record["since"] = since
        write_json(record_path, record)
        git("add", "-A")
        git(
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            f"Reflection {name}: the record, with the numbers it started from",
            check=False,
        )
        checks = subprocess.run(
            [str(worktree.path / ".nock" / "nock"), "check"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
        )
        if checks.returncode != 0:
            return {
                "landed": None,
                "changes": record.get("changes", []),
                "problem": f"checks fail on the round's branch (exit {checks.returncode}):\n{(checks.stdout + checks.stderr).strip()[:3000]}",
            }
        main = self.project.git
        with self.project.lock("merge"):
            if main.branch() != self.project.branch:
                raise PreconditionError(
                    f"the main checkout is on {main.branch()}, not {self.project.branch}; nothing landed"
                )
            try:
                main(
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    "--quiet",
                    "-m",
                    f"Reflection {name}\n\n{record.get('summary', '')}",
                    worktree.branch,
                )
            except GitError as error:
                main("merge", "--abort", check=False)
                raise ConflictError(
                    f"could not land {worktree.branch} (uncommitted changes in the way?): {error}"
                ) from error
        worktree.remove()
        return {
            "landed": main.head()[:12],
            "summary": record.get("summary"),
            "changes": record.get("changes", []),
        }


class Worktree:
    """The round's own branch and directory, beside the checkout — never inside
    it, where test runners and linters would find it."""

    def __init__(self, project: Project, name: str) -> None:
        self.project = project
        self.branch = f"nock/reflect-{name}"
        self.path = project.root.with_name(f"{project.root.name}.worktrees") / f"reflect-{name}"

    @property
    def git(self) -> Git:
        return Git(self.path)

    def create(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project.git("worktree", "prune")
        self.project.git(
            "worktree", "add", "--quiet", "-b", self.branch, str(self.path), self.project.branch
        )
        return self.path

    def remove(self) -> None:
        self.project.git("worktree", "remove", "--force", str(self.path), check=False)
        self.project.git("worktree", "prune", check=False)
        self.project.git("branch", "-D", self.branch, check=False)


def tool_rows(project: Project, since: str | None) -> list[dict[str, Any]]:
    """Per command over the window: calls, failures by kind, sessions, duration,
    how much it printed, how often it was capped or cached."""
    rows = project.store.rows("SELECT * FROM calls WHERE started > ? ORDER BY id", (since or "",))
    known = {tool.command.path: tool for tool in project.registry.tools}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["tool"] in known:
            grouped[row["tool"]].append(row)
    result = []
    for path, tool in known.items():
        calls = grouped.get(path, [])
        durations = [row["duration"] for row in calls if row["duration"] is not None]
        printed = [row["output_bytes"] or 0 for row in calls]
        codes = Counter(row["exit_code"] for row in calls)
        result.append(
            {
                "tool": path,
                "source": tool.source,
                "pinned": tool.pinned,
                "calls": len(calls),
                "sessions": len({row["session"] for row in calls if row["session"]}),
                "failures": sum(count for code, count in codes.items() if code not in (0, None)),
                "said_no": codes.get(1, 0),
                "usage_errors": sum(
                    count for code, count in codes.items() if code and 40 <= code < 50
                ),
                "machine_errors": sum(
                    count for code, count in codes.items() if code and code >= 50
                ),
                "median_seconds": round(statistics.median(durations), 2) if durations else None,
                "max_seconds": round(max(durations), 2) if durations else None,
                "mean_bytes": round(sum(printed) / len(printed)) if printed else None,
                "capped": sum(
                    1 for row in calls if (row["full_bytes"] or 0) > (row["output_bytes"] or 0)
                ),
                "cached": sum(row["cached"] or 0 for row in calls),
                "last_used": calls[-1]["started"] if calls else None,
            }
        )
    return result


class Material:
    """What a round reads, computed: the big picture first, then the numbers."""

    def __init__(self, project: Project, window: Window, last: dict[str, Any] | None) -> None:
        self.project = project
        self.window = window
        self.last = last
        self.since = window.since or ""
        identities = [row["id"] for row in window.sessions]
        marks = ",".join("?" for _ in identities) or "''"
        self.events = project.store.rows(
            f"SELECT * FROM events WHERE session IN ({marks}) ORDER BY session, moment",
            tuple(identities),
        )
        self.prompts = project.store.rows(
            f"SELECT * FROM prompts WHERE session IN ({marks}) ORDER BY moment", tuple(identities)
        )
        self.calls = project.store.rows(
            "SELECT * FROM calls WHERE started > ? ORDER BY id", (self.since,)
        )

    def render(self) -> str:
        sections = [
            self.overview(),
            self.requests(),
            self.direction(),
            self.expectations(),
            self.toolbox(),
            self.mistakes(),
            self.raw_work(),
            self.reading(),
            self.sessions_table(),
            QUERY_HELP,
        ]
        return "\n\n".join(section for section in sections if section)

    def overview(self) -> str:
        sessions = self.window.sessions
        tokens = sum(
            (row["input_tokens"] or 0)
            + (row["output_tokens"] or 0)
            + (row["cache_read_tokens"] or 0)
            + (row["cache_write_tokens"] or 0)
            for row in sessions
        )
        cost = sum(row["cost"] or 0 for row in sessions)
        since = (
            f"since the last round ({self.since})"
            if self.window.since
            else "since nock was installed"
        )
        return (
            f"# Overview\n\n{len(sessions)} sessions {since}: {len(self.prompts)} requests from the human, "
            f"{len(self.events)} tool calls, {len(self.calls)} calls to nock, "
            f"{tokens / 1e6:.1f}M tokens" + (f", ${cost:.2f}" if cost else "") + "."
        )

    def requests(self) -> str:
        """What the human asked for, in their words — the work the toolbox serves."""
        if not self.prompts:
            return ""
        titles = {row["id"]: row["title"] for row in self.window.sessions}
        lines = [
            "# What the human asked for",
            "",
            "In their words, oldest first; a correction or a repeat is a signal.",
        ]
        shown = self.prompts[-80:]
        if len(self.prompts) > len(shown):
            lines.append(f"(the {len(self.prompts) - len(shown)} oldest are left out)")
        current = None
        for prompt in shown:
            if prompt["session"] != current:
                current = prompt["session"]
                lines.append(f"\n## {(titles.get(current) or current)[:80]} ({current[:8]})")
            text = " ".join(prompt["text"].split())
            lines.append(f"- {text[:300]}{'…' if len(text) > 300 else ''}")
        return "\n".join(lines)

    def direction(self) -> str:
        """Where the code is going: commits and the areas they touch."""
        try:
            commits = self.project.git.commits(
                "HEAD",
                f"--since={self.window.since}" if self.window.since else "--max-count=60",
                limit=60,
            )
        except GitError:
            return ""
        if not commits:
            return ""
        areas: Counter[str] = Counter()
        churn: Counter[str] = Counter()
        for commit in commits:
            for path in commit.files:
                parts = path.split("/")
                area = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
                areas[area] += 1
            churn["insertions"] += commit.insertions
            churn["deletions"] += commit.deletions
        lines = [
            "# Where the code is going",
            "",
            f"{len(commits)} commits, +{churn['insertions']} −{churn['deletions']} lines. Most touched: "
            + ", ".join(f"{area} ({count})" for area, count in areas.most_common(10)),
            "",
        ]
        lines += [f"- {commit.moment[:10]} {commit.subject[:100]}" for commit in commits[:40]]
        return "\n".join(lines)

    def expectations(self) -> str:
        if not self.last:
            return "# The last round\n\nThis is the first round."
        lines = [
            f"# The last round ({self.last['round']})",
            "",
            str(self.last.get("summary", "")),
            "",
        ]
        before = self.last.get("snapshot") or {}
        now_rows = {row["tool"]: row for row in tool_rows(self.project, self.since)}
        for change in self.last.get("changes", []):
            lines.append(
                f"- {change.get('kind')} {change.get('target')}: expected {change.get('expect')}"
            )
            target = str(change.get("target", ""))
            for tool, row in now_rows.items():
                if tool == target or target.endswith(f"/{tool}") or f"`{tool}`" in target:
                    earlier = before.get(tool) or {}
                    lines.append(
                        f"  now: {row['calls']} calls in {row['sessions']} sessions, {row['failures']} failed"
                        + (
                            f" (the round before: {earlier.get('calls', 0)} calls, {earlier.get('failures', 0)} failed)"
                            if earlier
                            else ""
                        )
                    )
        if self.last.get("notes"):
            lines += ["", f"Notes it left: {self.last['notes']}"]
        return "\n".join(lines)

    def toolbox(self) -> str:
        rows = tool_rows(self.project, self.since)
        total = sum(row["calls"] for row in rows)
        threshold = 0.5 * math.log10(total) if total > 1 else 0
        lines = [
            "# The toolbox",
            "",
            "Per command over the window (nock's own only when used). said_no is exit 1, "
            "usage_errors 40s (called wrong), machine_errors 50s (broke); capped is how often it "
            "printed less than it had.",
            "",
            "tool,source,calls,sessions,said_no,usage_errors,machine_errors,median_seconds,max_seconds,mean_bytes,capped,cached,last_used",
        ]
        shown = [row for row in rows if row["calls"] or row["source"] != "nock"]
        for row in sorted(shown, key=lambda row: (row["source"] == "nock", -row["calls"])):
            lines.append(
                ",".join(
                    str(value if value is not None else "-")
                    for value in (
                        row["tool"],
                        row["source"],
                        row["calls"],
                        row["sessions"],
                        row["said_no"],
                        row["usage_errors"],
                        row["machine_errors"],
                        row["median_seconds"],
                        row["max_seconds"],
                        row["mean_bytes"],
                        row["capped"],
                        row["cached"],
                        (row["last_used"] or "-")[:10],
                    )
                )
            )
        rare = [
            row["tool"]
            for row in rows
            if row["source"] != "nock" and not row["pinned"] and row["calls"] < threshold
        ]
        if rare:
            lines += [
                "",
                f"Used fewer than {threshold:.1f} times (½·log₁₀ of all calls) — candidates to retire or merge: {', '.join(rare)}",
            ]
        pinned = [row["tool"] for row in rows if row["pinned"]]
        if pinned:
            lines.append(f"Pinned (keep however rarely used): {', '.join(pinned)}")
        failures = self.project.registry.failures
        if failures:
            lines += ["", "Tools that do not load:"] + [f"- {failure}" for failure in failures]
        return "\n".join(lines)

    def mistakes(self) -> str:
        """Calls nock refused or that broke: what agents expected to exist, or got wrong."""
        known = {tool.command.path for tool in self.project.registry.tools}
        unknown: Counter[str] = Counter()
        wrong: Counter[tuple[str, str]] = Counter()
        crashed: Counter[tuple[str, str]] = Counter()
        for row in self.calls:
            code = row["exit_code"] or 0
            if row["tool"] not in known:
                unknown[row["tool"]] += 1
            elif 40 <= code < 50 and row["error"]:
                wrong[(row["tool"], row["error"][:160])] += 1
            elif code >= 50 and row["error"]:
                crashed[(row["tool"], row["error"][:160])] += 1
        if not (unknown or wrong or crashed):
            return ""
        lines = ["# Mistakes", ""]
        if unknown:
            lines.append(
                "Commands agents tried that do not exist (what they expected to find): "
                + ", ".join(f"`{name}` ×{count}" for name, count in unknown.most_common(15))
            )
        for (tool, error), count in wrong.most_common(10):
            lines.append(f"- `{tool}` called wrong ×{count}: {error}")
        for (tool, error), count in crashed.most_common(10):
            lines.append(f"- `{tool}` broke ×{count}: {error}")
        return "\n".join(lines)

    def raw_work(self) -> str:
        """Shell work done around nock: what repeats, what fails, what prints most."""
        bash = [
            event
            for event in self.events
            if event["tool"] == "Bash" and not is_nock(event["input"] or "")
        ]
        if not bash:
            return ""
        by_shape: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in bash:
            by_shape[shape(event["input"] or "")].append(event)
        lines = [
            "# Raw shell work",
            "",
            "Commands run without nock, by what they are. trimmed = piped into head/tail/grep by hand.",
            "",
            "command,runs,sessions,failed,trimmed,median_seconds,total_output,example",
        ]
        ranked = sorted(by_shape.items(), key=lambda item: -len(item[1]))[:SECTION_ROWS]
        for name, events in ranked:
            durations = [event["duration"] for event in events if event["duration"] is not None]
            example = max(events, key=lambda event: len(event["input"] or ""))["input"].split("\n")[
                0
            ][:120]
            lines.append(
                ",".join(
                    [
                        name,
                        str(len(events)),
                        str(len({event["session"] for event in events})),
                        str(sum(event["failed"] or 0 for event in events)),
                        str(sum(1 for event in events if TRIMMERS.search(event["input"] or ""))),
                        seconds(statistics.median(durations) if durations else None),
                        kilobytes(sum(event["output_bytes"] or 0 for event in events)),
                        json.dumps(example),
                    ]
                )
            )
        sequences: Counter[tuple[str, str]] = Counter()
        previous: dict[str, str] = {}
        for event in self.events:
            if event["tool"] != "Bash":
                continue
            current = shape(event["input"] or "") if not is_nock(event["input"] or "") else "nock"
            before = previous.get(event["session"])
            if before and before != current:
                sequences[(before, current)] += 1
            previous[event["session"]] = current
        recurring = [(pair, count) for pair, count in sequences.most_common(8) if count >= 3]
        if recurring:
            lines += [
                "",
                "Recurring pairs (one command, then the next): "
                + "; ".join(f"{a} → {b} ×{count}" for (a, b), count in recurring),
            ]
        biggest = sorted(self.events, key=lambda event: -(event["output_bytes"] or 0))[:8]
        biggest = [event for event in biggest if (event["output_bytes"] or 0) > 4000]
        if biggest:
            lines += ["", "Largest outputs an agent had to read:"]
            lines += [
                f"- {kilobytes(event['output_bytes'])} — {event['tool']}: {(event['input'] or '')[:120]}"
                for event in biggest
            ]
        failing = [event for event in bash if event["failed"]]
        if failing:
            counted = Counter((event["input"] or "").split("\n")[0][:120] for event in failing)
            lines += ["", "Most repeated failing commands:"] + [
                f"- ×{count} {command}" for command, count in counted.most_common(8)
            ]
        return "\n".join(lines)

    def reading(self) -> str:
        """What agents read and searched for most: knowledge they keep rebuilding."""
        reads = Counter(event["input"] for event in self.events if event["tool"] == "Read")
        searches = Counter(
            event["input"] for event in self.events if event["tool"] in ("Grep", "Glob")
        )
        mix = Counter(event["tool"] for event in self.events)
        if not mix:
            return ""
        lines = [
            "# What agents read",
            "",
            "Tool calls by kind: "
            + ", ".join(f"{tool} {count}" for tool, count in mix.most_common(12)),
        ]
        repeated = [(path, count) for path, count in reads.most_common(15) if count > 2]
        if repeated:
            lines.append("Most read: " + ", ".join(f"{path} ×{count}" for path, count in repeated))
        searched = [(pattern, count) for pattern, count in searches.most_common(10) if count > 2]
        if searched:
            lines.append(
                "Most searched: "
                + ", ".join(f"{pattern!r} ×{count}" for pattern, count in searched)
            )
        return "\n".join(lines)

    def sessions_table(self) -> str:
        sessions = self.window.sessions
        if not sessions:
            return ""
        failed = Counter(event["session"] for event in self.events if event["failed"])
        calls = Counter(event["session"] for event in self.events)
        lines = [
            "# Sessions",
            "",
            "started,id,title,prompts,tool_calls,failed,interrupts,compactions,tokens,cost",
        ]
        for row in sessions[-SECTION_ROWS * 2 :]:
            tokens = sum(
                row[key] or 0
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            )
            lines.append(
                ",".join(
                    [
                        (row["started"] or "-")[:16],
                        row["id"][:8],
                        json.dumps((row["title"] or "")[:60]),
                        str(row["prompts"] or 0),
                        str(calls[row["id"]]),
                        str(failed[row["id"]]),
                        str(row["interrupts"] or 0),
                        str(row["compactions"] or 0),
                        f"{tokens / 1000:.0f}k",
                        f"${row['cost']:.2f}" if row["cost"] else "-",
                    ]
                )
            )
        return "\n".join(lines)


QUERY_HELP = """\
# Digging deeper

`.nock/nock query "SELECT …"` reads the telemetry (read-only SQLite). Tables:
calls (every nock call: tool, arguments, session, started, duration,
exit_code, output_bytes, full_bytes, cached, error), runs (supervised
processes, their logs in .nock/local/logs/run-ID.log), sessions, prompts
(session, moment, text), events (session, moment, tool, input, failed,
output_bytes, duration — every tool call of every session). Example:
`.nock/nock query "SELECT input, output_bytes FROM events WHERE tool = 'Bash' AND input LIKE '%pytest%' ORDER BY output_bytes DESC LIMIT 5"`."""
