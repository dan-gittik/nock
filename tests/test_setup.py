from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from nock.project import Project
from nock.setup import PACKAGE, Installer, Upgrader

from .conftest import git


def test_init_is_idempotent_and_keeps_the_project_s_settings(repository: Path) -> None:
    settings = repository / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}, "model": "x"}
        )
    )
    (repository / "CLAUDE.md").write_text("# Rules\n\nBe kind.\n")
    Installer(repository).install()
    wired = json.loads(settings.read_text())
    assert wired["model"] == "x"
    commands = [hook["command"] for entry in wired["hooks"]["Stop"] for hook in entry["hooks"]]
    assert commands == ["mine"]
    started = [
        hook["command"] for entry in wired["hooks"]["SessionStart"] for hook in entry["hooks"]
    ]
    assert started == ['"$CLAUDE_PROJECT_DIR"/.nock/nock hook SessionStart']
    assert (repository / "CLAUDE.md").read_text() == "# Rules\n\nBe kind.\n\n@.nock/context.md\n"
    shutil.rmtree(repository / ".nock" / "core")
    Installer(repository).install()
    again = json.loads(settings.read_text())
    assert again == wired
    assert (repository / "CLAUDE.md").read_text().count("@.nock/context.md") == 1


def test_the_vendored_shim_runs_the_project_s_own_copy(project: Project) -> None:
    marker = project.root / ".nock" / "core" / "nock" / "builtin.py"
    marker.write_text(
        marker.read_text().replace('"""How to write a tool', '"""VENDORED How to write a tool')
    )
    result = subprocess.run(
        [str(project.root / ".nock" / "nock")],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "guide — VENDORED" in result.stdout


def test_upgrade_merges_upstream_into_a_locally_edited_core(
    project: Project, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    shutil.copytree(
        PACKAGE, upstream / "src" / "nock", ignore=shutil.ignore_patterns("__pycache__")
    )
    (upstream / "pyproject.toml").write_text('version = "0.2.0"\n')
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "base")
    base = git(upstream, "rev-parse", "HEAD")
    core = project.root / ".nock" / "core"
    shutil.rmtree(core / "nock")
    shutil.copytree(upstream / "src" / "nock", core / "nock")
    (core / "VERSION.json").write_text(
        json.dumps({"version": "0.1.0", "source": str(upstream), "commit": base})
    )
    ours = core / "nock" / "utils.py"
    ours.write_text(ours.read_text() + "\n\ndef local_helper() -> int:\n    return 1\n")
    theirs = upstream / "src" / "nock" / "utils.py"
    theirs.write_text(
        '"""Upstream changed the docstring."""\n' + theirs.read_text().split("\n", 1)[1]
    )
    (upstream / "src" / "nock" / "new_module.py").write_text("VALUE = 2\n")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "next")
    report = Upgrader(project).upgrade()
    assert not report.conflicts
    merged = ours.read_text()
    assert merged.startswith('"""Upstream changed the docstring."""') and "local_helper" in merged
    assert (core / "nock" / "new_module.py").read_text() == "VALUE = 2\n"
    assert json.loads((core / "VERSION.json").read_text())["version"] == "0.2.0"
