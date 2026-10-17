"""Running processes for an agent: supervised, bounded, condensed — and the
cache and the repeat note that save it re-running what has not changed."""

from __future__ import annotations

from pathlib import Path

from nock.condense import clean, excerpt
from nock.project import Project

from .conftest import git, nock, write_tool

TOOLS = '''\
import nock


@nock.command(cache="data/**")
def total(root, sh) -> str:
    """Add up the data, slowly."""
    sh("sleep 0.3")
    return str(sum(int(path.read_text()) for path in sorted((root / "data").glob("*.txt"))))


@nock.command
def failing(sh) -> None:
    """Fail through sh."""
    sh("echo one; echo two; ls definitely-missing")


@nock.command
def missing(sh) -> None:
    """Run a command that is not there."""
    sh(["definitely-not-a-program-xyz"])


@nock.command
def slow(sh, *, deadline: float = 0.5, silence: float = 10) -> None:
    """Outlive a bound."""
    sh("sleep 3", deadline=deadline, silence=silence)
'''


def test_condensing_drops_terminal_noise_and_folds_repeats(tmp_path: Path) -> None:
    raw = (
        "\x1b[1m\x1b[31mERROR\x1b[0m in "
        f"{tmp_path}/src/app.py\n"
        "Downloading 10%\rDownloading 55%\rDownloading 100%\n"
        "same\nsame\nsame\n\n\n\n"
        "item 1 of 9\nitem 2 of 9\nitem 3 of 9\nitem 4 of 9\n"
        "done\n"
    )
    assert clean(raw, tmp_path) == [
        "ERROR in src/app.py",
        "Downloading 100%",
        "same (×3)",
        "",
        "item 4 of 9 (×4 similar)",
        "done",
    ]


def test_an_excerpt_keeps_the_end_and_says_what_it_left_out() -> None:
    lines = [f"line {index}" for index in range(1000)]
    text, cut = excerpt(lines, 400)
    assert cut and text.startswith("line 0\n") and text.endswith("line 999")
    assert "lines omitted" in text and len(text) <= 440
    assert excerpt(["short"], 400) == ("short", False)


def test_run_supervises_any_command_and_passes_its_code_on(project: Project) -> None:
    script = project.root / "noisy.sh"
    script.write_text('for i in $(seq 1 300); do echo "step $i"; done\necho finished\nexit 3\n')
    result = nock(project.root, "run", "--", "bash", "noisy.sh")
    assert result.returncode == 3
    assert result.stdout == "step 300 (×300 similar)\nfinished\n"
    run = project.store.rows("SELECT * FROM runs")[0]
    assert run["exit_code"] == 3 and run["call"] is not None
    assert (project.logs / f"run-{run['id']}.log").read_text().count("step") == 300


def test_sh_turns_failures_into_exit_codes(project: Project) -> None:
    write_tool(project, "work", TOOLS)
    failing = nock(project.root, "failing")
    assert failing.returncode == 1
    assert failing.stdout.startswith("one\ntwo\nls: definitely-missing")
    assert "exited 1" in failing.stderr
    missing = nock(project.root, "missing")
    assert missing.returncode == 42 and "could not run" in missing.stderr
    assert nock(project.root, "slow").returncode == 54
    assert nock(project.root, "slow", "--deadline", "60", "--silence", "0.5").returncode == 55


def test_a_repeated_failure_says_so_and_whether_anything_changed(project: Project) -> None:
    write_tool(project, "work", TOOLS)
    first = nock(project.root, "failing")
    assert "same result" not in first.stderr
    second = nock(project.root, "failing")
    assert "same result as call 1" in second.stderr and "no file has changed since" in second.stderr
    (project.root / "touched.txt").write_text("x\n")
    third = nock(project.root, "failing")
    assert "same result as call 2" in third.stderr and "no file has changed" not in third.stderr


def test_a_cached_tool_replays_while_its_inputs_stand_still(project: Project) -> None:
    write_tool(project, "work", TOOLS)
    data = project.root / "data"
    data.mkdir()
    (data / "a.txt").write_text("2\n")
    (data / "b.txt").write_text("3\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-qm", "data")
    assert nock(project.root, "total").stdout == "5\n"
    replay = nock(project.root, "total")
    assert replay.stdout == "5\n" and "cached" in replay.stderr
    (data / "b.txt").write_text("30\n")
    assert nock(project.root, "total").stdout == "32\n"
    fresh = nock(project.root, "total", "--fresh")
    assert fresh.stdout == "32\n" and "cached" not in fresh.stderr
    calls = project.store.rows("SELECT cached FROM calls WHERE tool = 'total' ORDER BY id")
    assert [row["cached"] for row in calls] == [0, 1, 0, 0]
