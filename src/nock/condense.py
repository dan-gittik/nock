"""Condensing a process's output for an agent to read: terminal noise out
(colors, progress bars redrawn in place), absolute paths made relative,
repeated lines folded into one with a count, and — when it is still too long —
an excerpt that keeps the end, where results and errors are, with the full log
a path away."""

from __future__ import annotations

import re
from pathlib import Path

# CSI and OSC escape sequences, and the stray control characters terminals use.
ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DIGITS = re.compile(r"\d+")
# Consecutive lines alike but for their numbers fold once there are this many.
SIMILAR_RUN = 3


def clean(text: str, root: Path | None = None) -> list[str]:
    """The lines a reader needs: no escapes, no overdrawn progress, paths
    relative to `root`, no trailing space, blank runs squeezed to one."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rsplit("\r", 1)[-1] if "\r" in raw else raw
        line = CONTROL.sub("", ESCAPE.sub("", line)).rstrip()
        if root is not None:
            line = line.replace(f"{root}/", "")
        if not line and lines and not lines[-1]:
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return fold(lines)


def fold(lines: list[str]) -> list[str]:
    """Identical consecutive lines become one with `(×N)`; lines alike but for
    their numbers (a counter, a download) become the last of them with
    `(×N similar)`."""
    folded: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        end = index + 1
        while end < len(lines) and lines[end] == line:
            end += 1
        if end - index > 1:
            folded.append(f"{line} (×{end - index})" if line else line)
            index = end
            continue
        shape = DIGITS.sub("#", line)
        end = index + 1
        while end < len(lines) and DIGITS.sub("#", lines[end]) == shape and shape != line:
            end += 1
        if end - index >= SIMILAR_RUN:
            folded.append(f"{lines[end - 1]} (×{end - index} similar)")
            index = end
            continue
        folded.append(line)
        index += 1
    return folded


def excerpt(lines: list[str], characters: int, head_share: float = 0.25) -> tuple[str, bool]:
    """The lines whole when they fit in `characters`; otherwise a head and a
    longer tail around a line saying how much was left out. Returns the text
    and whether anything was."""
    text = "\n".join(lines)
    if len(text) <= characters:
        return text, False
    head_budget = int(characters * head_share)
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > characters - head_budget:
            break
        tail.insert(0, line)
        used += len(line) + 1
    if not head and not tail:
        return text[: characters // 4] + "\n…\n" + text[-(characters * 3) // 4 :], True
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"… {omitted} lines omitted …", *tail]), True
