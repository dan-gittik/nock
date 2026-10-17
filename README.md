# nock

A toolbox your coding agents build for themselves.

## Why

Agents repeat themselves:

- they run the same five shell commands to find out why a test fails;
- they write the same throwaway script to query the database;
- they pipe the same noisy output through `tail`;
- they trip over the mistake the human has already corrected twice.

Every repeat costs turns and tokens, and every session starts again from
nothing.

The usual fix is prose: skills, notes, rules in CLAUDE.md. Prose has two
problems:

- **It goes stale without anyone noticing.** When the code moves on, a
  paragraph that no longer fits the project still reads as true.
- **It is read differently every time.** An agent can follow it, skip it, or
  half-apply it, and nobody can tell which.

nock's fix is code. A command that no longer fits the project **breaks**:
it exits non-zero, a check fails, and the failure is recorded. So the toolbox
can be kept honest by measuring it. That is also the rule nock lives by:

> **A check beats a hook beats prose.**
>
> A rule that a check enforces cannot be ignored. A behavior that a hook
> triggers cannot be forgotten. Prose is the last resort.

### Three principles

1. **An agent-friendly CLI.** Every byte a command prints is read by a model
   and paid for, so:
   - a command prints what the next decision needs, in the cheapest format
     that reads accurately;
   - success is often silence: a passing check prints nothing, and its exit
     code says so;
   - the exit code says what to do next: change the request, fix the code, or
     fix the machine;
   - a mistake gets an answer, not just a refusal: "did you mean", the
     expected shape of the input, a path to the full log.
2. **Every run recorded.** Every call to nock is recorded locally: its
   arguments, duration and exit code, and how much the agent had to read. So
   is every Claude Code session: what the human asked, each tool call the
   agent made (nock or not), what failed, and what it cost. You cannot
   improve what you have not measured, and agents do not remember.
3. **Reflection.** Every so often, an agent reads that record and improves
   the toolbox:
   - it builds tools for the work that keeps costing turns and tokens;
   - it repairs and retunes the tools that misbehave;
   - it retires the tools nobody uses.

   It starts from the big picture, not just the numbers: what the human has
   been asking for, and where the code is going. It writes down what it
   expects each change to do, and the next round holds it to that. What it
   builds is live on the next call.

### A framework, not a library

A tool's author writes only what the tool does. nock owns everything around
it, the way a web framework owns HTTP or pytest owns setup and teardown:

- parsing the arguments and checking the input;
- rendering the output, and capping it;
- exit codes and error reports;
- running processes under supervision;
- caching;
- telemetry;
- help text.

That matters most when an agent writes tools for itself. It has to get only
the business logic right, and everything else comes out the same in every
tool.

nock is standard-library Python 3.12 and has no dependencies. It is vendored
into the project, so it is the project's own code: an agent can read it, and
fix it if it must.

## Getting started

From a checkout of nock, inside the repository you want it in:

```sh
uv run --project ~/path/to/nock nock init
```

`nock init`:

- vendors nock into `.nock/core/` and writes the `.nock/nock` shim;
- writes `.nock/config.toml` and an empty `.nock/tools/`;
- writes the primer `.nock/context.md`, and imports it into CLAUDE.md;
- wires Claude Code's hooks;
- allows `.nock/nock` in Claude Code's permissions.

Commit the result. From then on, every agent session in the repository starts
with the project's tools listed, and is recorded when it ends.

To make your test suite and linters the gate that tools and reflection rounds
must pass, add them as checks:

```toml
# .nock/config.toml
[checks.test]
command = "pytest -q"

[checks.lint]
command = "ruff check"
```

## A tool

```python
# .nock/tools/tests.py
import nock


@nock.command(cache="src/** tests/**", examples=["--path tests/unit"])
def failures(sh, *, path: str = "tests") -> list[dict]:
    """The failing tests, with the first line of each error."""
    run = sh(f"python -m pytest -q -rf {path}", check=False)
    return [{"test": test, "error": error} for test, error in parse(run.lines)]
```

```sh
$ .nock/nock failures --path tests/unit
test,error
tests/unit/test_cart.py::test_total,assert 41 == 42
tests/unit/test_cart.py::test_empty,KeyError: 'items'
```

The tool works on its next call, with nothing to register. `.nock/nock guide`
is the whole framework on one page, written for agents. In short:

**The signature is the command line.**

- A positional parameter is an argument, and a keyword-only one is an
  `--option`. A `bool` is a flag, a `list` repeats, and an `Enum` or
  `Literal` gives the choices.
- A parameter typed as a TypedDict or a dataclass is one JSON argument. It
  can be given inline, as a path to a JSON file, or on stdin. It is checked
  against the type, and a misspelled key gets a "did you mean".
- The docstring is the help, and `group="db"` makes the tool
  `.nock/nock db NAME`.

**Fixtures, as in pytest.** A tool asks for what it needs by naming a
parameter after it:

| fixture | is |
|---|---|
| `sh` | runs a command supervised: bounded by a deadline and a silence timeout, logged, and condensed |
| `root` | the working tree |
| `git` | `git("log", "-1")` returns the output |
| `changed` | the files changed against the main branch |
| `tmp` | a directory, removed after the call |
| `log` | notes to the call's log file, never to the caller |
| `config`, `project` | the settings, and the project |

`@nock.fixture` defines your own, and a `yield` in it cleans up after the
call.

**Return data, and nock prints it.**

| the tool returns | nock prints |
|---|---|
| `None` | nothing: the exit code says it |
| `False` | nothing, and exits 1 |
| a string | the string |
| a dict | `key: value` lines |
| a list | one item per line |
| a list of dicts | CSV under a header; a column with the same value in every row is printed once, above the table |
| nested data | indented `key: value` |
| an `Output` from `sh` | the command's condensed output, and its exit code |

These are the cheapest formats that models still read accurately. Long output
is capped at about 8,000 characters or 60 table rows, and the rest is saved to
a file whose path is printed. `--json` gives JSON, and `--full` gives
everything.

**Fail by raising.**

- `nock.Failed("why", evidence)` exits 1, and the evidence prints like a
  return value.
- `nock.UsageError` exits 40, `nock.PreconditionError` 42 and
  `nock.NotFoundError` 44.
- A crash prints the lines of the tool's own code that it went through, with
  the values those lines used, and leaves nock's frames out.

**Caching.** With `cache="src/**"`, a call with the same arguments replays the
last result as long as those files and the tool's own code are unchanged.
`--fresh` runs the tool anyway.

**Examples.** The strings in `examples=[...]` appear in `--help`, and
`nock check` runs them, so a tool that breaks fails the gate.

### Checks, hooks and generators

**Checks** are functions that return their problems, as strings or as
`nock.Problem(message, path, line)`. They return nothing when all is well.

```python
@nock.check
def migrations_reversible(root, changed):
    """Every new migration has a down step."""
    return [
        nock.Problem("no down()", path)
        for path in changed
        if path.startswith("migrations/") and "def down" not in (root / path).read_text()
    ]
```

A check that asks for `changed` applies its rule to new code only. So it can
land while old code still breaks the rule. A command can be a check too, set
in `.nock/config.toml` as shown above.

**Hooks** react to Claude Code's events: `session-start`, `prompt`,
`after-tool`, `tool-failed`, `stop` and `session-end`.

- A hook asks for `payload`, the event's JSON.
- Text a hook returns is added to what the agent reads.
- `raise nock.Block("why")` at `stop` refuses the end of the turn.

Hooks run on every event of every session, so prefer a check.

**Generators** keep derived files in step with their source:

- `@nock.generator("src/version.py")` returns the text of one file.
- A **catalog** (`nock gen new`) is a list that code is generated from, such
  as error codes, feature flags or strings. Its entries are checked against a
  schema and rendered through templates into every file that lists them.
  `nock gen add flags '{"key": "dark_mode", …}'` adds an entry and rewrites all of those
  files in one call.

`nock gen write` writes the generated files, and `nock check` fails when one
of them has drifted.

## What agents see

Every session starts with the project's tools listed, through Claude Code's
SessionStart hook. CLAUDE.md imports a short primer. `.nock/nock` with no
arguments lists the toolbox. A mistake gets an answer:

```sh
$ .nock/nock failurs
nock: no command `failurs` — did you mean `failures`? (`.nock/nock` lists them all)
$ .nock/nock failures --pth tests
nock: unrecognized arguments: --pth tests; --pth → did you mean --path?
```

For work that isn't a tool yet:

- **`.nock/nock run -- COMMAND`** runs anything long or noisy under
  supervision. Colors and redrawn progress bars are dropped, repeated lines
  are folded into one with a count, and long output is cut to its end, with
  the log's path.
- **A failing call that repeats an earlier one byte for byte says so**: "same
  result as call 41 (3m ago) and no file has changed since". It never refuses
  to run, because something outside the repository may have changed.

## Telemetry

Everything is in `.nock/local/nock.db`, a local SQLite file that is never
committed:

| table | holds |
|---|---|
| `calls` | every nock call, including refused ones: the command, its arguments, session, duration, exit code, bytes printed and in full, whether it was cached, and the error |
| `runs` | every supervised process; the logs are in `.nock/local/logs/` |
| `sessions` | each Claude Code session's title, tokens, cost, prompts, interrupts and compactions |
| `prompts` | what the human typed |
| `events` | every tool call of every session: the command, path or pattern, whether it failed, the bytes returned, and the duration |

Sessions are read from Claude Code's transcripts when they end, and any
that were missed are read when a reflection round starts. To look:

- `nock calls`, `nock stats` and `nock sessions` summarize the data;
- `nock query "SELECT …"` runs read-only SQL.

## Reflection

When a round is due, the session start says so: by default after 15 sessions,
or after a week with at least 3. `nock reflect run` takes a while, so run it
in the background.

1. **The material.** nock computes it from the telemetry and git, starting
   with the big picture:
   - what the human asked for, in their words, including corrections and
     repeated requests;
   - where the code is going: the commits and the areas they touch;
   - the last round's expectations, next to the numbers since.

   Then the numbers:
   - each tool's calls, failures by kind, duration, bytes printed, and how
     often it was capped or cached;
   - tools that are candidates to retire (the threshold rises with traffic);
   - commands agents tried that do not exist;
   - raw shell work: what recurs, what fails, what agents trimmed by hand
     with `| tail`, which pairs of commands recur, and the largest outputs;
   - which files agents read again and again.
2. **The round.** An agent gets the material and the brief. It works in a
   worktree on its own branch, and changes only `.nock/`:
   - it adds, changes or retires tools (retired ones go to
     `.nock/tools/archive/`, which is not loaded);
   - it adds checks and generators;
   - or it changes nothing, when nothing earns a change.

   It commits each change.
3. **The record.** `nock reflect record` writes `.nock/reflections/STAMP.json`.
   The record holds:
   - the round's summary;
   - each change, with its evidence and what should measurably change by the
     next round;
   - verdicts on the last round's expectations.

   nock adds a snapshot of each tool's numbers. There is one file per round,
   so rounds from different machines merge.
4. **Landing.** nock runs `nock check` on the branch, which includes every
   tool's examples. If the checks pass, the branch is merged into the main
   branch. If they fail, or the agent left no record, nothing lands, and the
   branch and worktree are kept to look at.

`nock reflect material` prints what the next round would read, and
`nock reflect due` says whether a round is due. A project can replace the
brief with its own `.nock/briefs/reflect.md`.

## Reference

### Commands

| command | does |
|---|---|
| `nock` | the toolbox: the project's tools first, then nock's |
| `nock check [NAME…] [--fix]` | run the checks; prints nothing when all pass |
| `nock run -- COMMAND` | run anything supervised and condensed |
| `nock guide` | how to write a tool, check, hook or generator |
| `nock calls`, `stats`, `sessions`, `query SQL` | the telemetry |
| `nock reflect run`, `due`, `material`, `record JSON` | reflection |
| `nock gen write`, `new`, `add`, `set`, `rm`, `show`, `bind`, `unbind` | generators |
| `nock init`, `nock upgrade` | install (or re-wire), and upgrade the vendored core with a three-way merge |

Every command takes `--json` and `--full`, and every cached command also takes
`--fresh`.

### Exit codes

The codes are modelled on HTTP's, and each one tells the agent what to do
next.

| code | meaning | do |
|---|---|---|
| 0 | yes | — |
| 1 | no: the subject failed (a check, a test) | fix the code |
| 40 | called wrong | change the request |
| 41 | not authenticated | log in |
| 42 | the machine or the state is not ready | fix that first, never the code |
| 43 | policy | don't |
| 44 | not found | check the reference |
| 49 | conflict | reconcile |
| 50 | nock or a tool crashed | report it, or fix the tool |
| 52 | an external service failed | retry later |
| 54 | timed out | raise the bound or split the work |
| 55 | hung: no output for too long | suspect a deadlock |
| 56 | killed by nock | see the log |

### Settings

`.nock/config.toml` is committed. `.nock/local/config.toml` overrides it on
one machine.

| key | default | is |
|---|---|---|
| `branch` | `main` | where reflection rounds land |
| `output.characters`, `output.rows` | 8000, 60 | the cap on what a call prints |
| `run.deadline`, `run.silence` | 3600, 900 | seconds before a supervised process is stopped |
| `checks.NAME.command` | — | a command that is a check |
| `checks.concurrency` | 2 | checks run at once, machine-wide |
| `reflect.sessions`, `reflect.days`, `reflect.minimum` | 15, 7, 3 | when a round is due |
| `reflect.command`, `reflect.model` | `claude`, — | the agent a round dispatches |
| `pricing` | — | per-model prices, to cost sessions |

## Developing nock

See [ARCH.md](ARCH.md) for how the code is laid out.

```sh
uv sync
uv run pytest -n 4        # real repositories and processes, no mocks
uv run ruff check && uv run basedpyright
uv run nock init          # in another repository: vendor this checkout into it
```

`evals/` measures whether reflection pays; see `evals/README.md`.
