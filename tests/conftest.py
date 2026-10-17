"""Real repositories, real processes, real SQLite: a project is a fresh git
repository with nock installed into it, and the reflecting agent is a scripted
stand-in process."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from nock.project import Project
from nock.setup import Installer

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=directory, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No Claude Code session, nock seat or git identity leaks in from outside."""
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "NOCK_", "GIT_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    yield


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# demo\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def project(repository: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    """A repository with nock installed and committed, the agent a stand-in."""
    Installer(repository).install()
    local = repository / ".nock" / "local"
    local.mkdir(exist_ok=True)
    (local / "config.toml").write_text(
        f'[reflect]\ncommand = ["{sys.executable}", "{FAKE_AGENT}"]\n[run]\nheartbeat = 0\n'
    )
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "Add nock")
    monkeypatch.chdir(repository)
    return Project.discover(repository)


def nock(
    directory: Path, *arguments: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The CLI, run as an agent runs it: the project's vendored copy."""
    return subprocess.run(
        [str(directory / ".nock" / "nock"), *arguments],
        cwd=directory,
        capture_output=True,
        text=True,
        input=stdin,
        env={**os.environ, "NOCK_PYTHON": sys.executable},
    )


def write_tool(project: Project, name: str, source: str) -> None:
    """A module in the project's `.nock/tools/`, committed."""
    path = project.root / ".nock" / "tools" / f"{name}.py"
    path.write_text(source)
    git(project.root, "add", "-A")
    git(project.root, "commit", "-qm", f"Add tool {name}")
