"""Git, wrapped once: every call raises a typed error carrying the command and
its output."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from nock.errors import PreconditionError

# The empty tree, for a diff against nothing.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(PreconditionError):
    """A git command that did not do what its caller required."""

    def __init__(self, command: list[str], output: str, directory: Path) -> None:
        self.command = command
        self.output = output
        self.directory = directory
        super().__init__(f"`git {' '.join(command)}` failed in {directory}: {output.strip()}")


@dataclass(frozen=True)
class Commit:
    """One commit as nock reads it: identity, moment, subject and size."""

    sha: str
    moment: str
    author: str
    subject: str
    body: str
    parents: tuple[str, ...]
    files: tuple[str, ...] = ()
    insertions: int = 0
    deletions: int = 0


class Git:
    """One working tree's git."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def __call__(self, *arguments: str, check: bool = True, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.directory,
            capture_output=True,
            text=True,
            input=input_text,
        )
        if check and result.returncode != 0:
            raise GitError(list(arguments), result.stderr + result.stdout, self.directory)
        return result.stdout.rstrip()

    def succeeds(self, *arguments: str) -> bool:
        return (
            subprocess.run(["git", *arguments], cwd=self.directory, capture_output=True).returncode
            == 0
        )

    # --- where we are ---------------------------------------------------------

    def toplevel(self) -> Path:
        return Path(self("rev-parse", "--show-toplevel"))

    def main_checkout(self) -> Path:
        """The main working tree of the repository, even from a linked worktree."""
        common = self("rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(common).parent

    def branch(self) -> str:
        return self("rev-parse", "--abbrev-ref", "HEAD")

    def head(self, reference: str = "HEAD") -> str:
        return self("rev-parse", reference)

    def dirty(self, *paths: str) -> list[str]:
        """The paths with uncommitted changes, untracked files included."""
        output = self("status", "--porcelain", "--untracked-files=all", "--", *paths)
        return [line[3:] for line in output.splitlines() if line]

    # --- reading history ------------------------------------------------------

    def commits(
        self, revision_range: str = "HEAD", *options: str, limit: int | None = None
    ) -> list[Commit]:
        """Commits in `revision_range`, newest first, with their numstat."""
        separator = "\x1e"
        field = "\x1f"
        arguments = [
            "log",
            f"--format={separator}%H{field}%aI{field}%an{field}%P{field}%s{field}%b{field}",
            "--numstat",
            *options,
        ]
        if limit is not None:
            arguments.append(f"--max-count={limit}")
        arguments.append(revision_range)
        if not self.succeeds("rev-parse", "--verify", "--quiet", "HEAD"):
            return []
        output = self(*arguments)
        commits = []
        for chunk in output.split(separator):
            if not chunk.strip():
                continue
            parts = chunk.split(field)
            sha, moment, author, parents, subject, body, numstat = parts[:7]
            files, insertions, deletions = [], 0, 0
            for line in numstat.strip().splitlines():
                columns = line.split("\t")
                if len(columns) != 3:
                    continue
                added, removed, path = columns
                files.append(path)
                insertions += int(added) if added.isdigit() else 0
                deletions += int(removed) if removed.isdigit() else 0
            commits.append(
                Commit(
                    sha=sha,
                    moment=moment,
                    author=author,
                    subject=subject,
                    body=body.strip(),
                    parents=tuple(parents.split()),
                    files=tuple(files),
                    insertions=insertions,
                    deletions=deletions,
                )
            )
        return commits
