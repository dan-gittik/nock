# Architecture

This document covers how nock is built: the repository, the layers, what
happens on each path through the code, and where a project plugs in. The
README covers what nock is for and how to use it.

## Constraints that shape everything

- **Vendored.** `nock init` copies `src/nock/` into a project's
  `.nock/core/nock/`, and the `.nock/nock` shim runs it with
  `PYTHONPATH=.nock/core python3 -m nock`. So the package must run on bare
  Python 3.12 with the standard library and nothing else. That is why nock
  has its own template engine (`templates.py`) and its own JSON Schema subset
  (`schema.py`), and why `pyproject.toml` has no runtime dependencies.
- **One process per call.** Every `.nock/nock …` call is a fresh interpreter.
  Nothing is held in memory between calls: state is in SQLite and in files
  under `.nock/local/`, and caches are keyed on content.
- **Telemetry on the main checkout, code in the working tree.** A reflection
  round runs in a git worktree. Its calls must use that worktree's tools, but
  must record into the one database. `Project` holds both places: `root` is
  the main checkout, and `here` is the working tree the call runs in.
- **nock eats its own cooking.** nock's own commands, checks and fixtures are
  registered with the same decorators a project uses. `builtin.py` and
  `fixtures.py` are effectively the first two modules of every toolbox.

## Repository

```
src/nock/
  __init__.py        the public API: decorators, error classes, Output, Problem, Block
  __main__.py        python -m nock → cli.main

  # the framework: from words typed to an exit code
  cli.py             the weaver: one invocation, end to end
  commands.py        function signature → argparse command; records, schemas, suggestions
  tools.py           the decorators, and the Registry that loads the toolbox
  fixtures.py        Scope (resolves fixtures by name) and nock's own fixtures: sh, root, git, …
  render.py          return value → text (CSV, key: value, …); caps; crash reports
  errors.py          the ExitCode taxonomy and the error class for each code
  processes.py       Supervisor: run a process with a deadline and a silence timeout; Output
  condense.py        clean, fold and excerpt process output

  # telemetry
  store.py           the SQLite schema and a thin Store over it
  telemetry.py       Calls (start, finish, the repeated-failure note) and Cache
  sessions.py        Claude Code transcripts → sessions, prompts and events

  # the loop
  reflect.py         Reflection (due, material, run, land), Rounds, Material, Worktree
  dispatch.py        Dispatcher: launch a headless `claude -p` session and ingest it
  hooks.py           `nock hook EVENT`: nock's own handlers, then the project's
  briefs/
    reflect.md       the reflecting agent's brief (a template)
    _framework.md    the framework on one page: `nock guide`, included in the brief

  # the rest of the toolbox
  checks.py          Checks: command checks and @check functions → one Verdict
  generators.py      @generator files and catalogs; drift detection
  templates.py       the template engine used by catalogs and briefs
  schema.py          JSON Schema subset validation
  builtin.py         nock's own commands and checks, as tools

  # plumbing
  project.py         Project: the hub (places, configuration, git, registry, store, locks)
  configuration.py   defaults ⊕ .nock/config.toml ⊕ .nock/local/config.toml
  git.py             Git: one wrapper, typed errors
  setup.py           Installer (nock init) and Upgrader (three-way merge of the core)
  utils.py           time, atomic writes, JSON, slugs

tests/               real git repositories and processes; no mocks
  conftest.py        a fresh repository with nock installed, per test; the `nock()` runner
  fake_agent.py      the scripted reflecting agent (FAKE_REFLECT=add|silent|broken)
evals/               does reflection pay? real sessions, with and without a toolbox
```

## The layers

```
             ┌─────────────────────────────────────────────────────┐
  the loop   │ hooks.py   reflect.py ── dispatch.py ── briefs/       │
             ├─────────────────────────────────────────────────────┤
 telemetry   │ telemetry.py   sessions.py   store.py (nock.db)      │
             ├─────────────────────────────────────────────────────┤
 framework   │ cli.py ── commands.py  tools.py  fixtures.py         │
             │           render.py  errors.py  processes.py         │
             ├─────────────────────────────────────────────────────┤
  toolbox    │ builtin.py  checks.py  generators.py  .nock/tools/*  │
             ├─────────────────────────────────────────────────────┤
  plumbing   │ project.py  configuration.py  git.py  utils.py …     │
             └─────────────────────────────────────────────────────┘
```

The framework knows nothing about reflection. Reflection reads only what the
framework and the hooks recorded. A project's tools sit at the same level as
nock's built-ins.

## The life of a call

`.nock/nock failures --path tests` goes through `cli.Invocation.run`:

1. **Discover.** `Project.discover()` finds `here` (`git rev-parse
   --show-toplevel`) and `root` (the main checkout). It then requires
   `.nock/` to exist.
2. **Load.** `Registry.load(project)` imports `nock.builtin` and
   `nock.fixtures`, then every `.nock/tools/*.py` (subdirectories such as
   `archive/` are skipped). Each decorator appends to a pending list, which the
   registry takes as it imports each module, so every tool knows its source
   file. A module that fails to import, or a name that clashes, goes into
   `registry.failures` with `file:line` and is printed on every call. The
   other tools keep working.
3. **Parse.** `cli.build()` turns the registry into an argparse tree, through
   `commands.Command.configure`. Each parameter is classified as a `Kind`:
   - a **fixture**, when its name is a known fixture; it is not on the
     command line;
   - a **record**, when its type is a TypedDict, a dataclass or a dict; it is
     one JSON argument;
   - an **argument**, when it is positional;
   - an **option**, when it is keyword-only;
   - **rest**, for `run -- COMMAND`.

   A parse error raises `UsageError`. Before that, `Group.suggest` or
   `unrecognized` adds a "did you mean". The refusal is still recorded as a
   call, so the material shows what agents tried.
4. **Cache.** If the tool has `cache=`, `telemetry.Cache.key` hashes:
   - the command's path and its arguments;
   - the bytes of the tool's source file;
   - the size and mtime of every file its globs match.

   On a hit, the stored result is replayed with a note, and step 5 is
   skipped.
5. **Execute.** `fixtures.Scope` resolves the function's fixture parameters
   by name, recursively, with cycle detection. A generator fixture is entered
   and its teardown is queued. The result becomes an `Outcome`:
   - an `Error` becomes its exit code, and its `value` is printed as
     evidence;
   - `Exit` passes a code through;
   - any other exception becomes 50, with `render.crash` showing the tool's
     own frames and the values in them;
   - `False` becomes 1.
6. **Render.** `render.render` picks the format by the result's shape. An
   `Output` from `sh` uses its own excerpt, which keeps the end. `--json`
   bypasses rendering. `render.capped` cuts to the configured characters or
   rows. The full text goes to `.nock/local/outputs/call-N.txt`, and its path
   is printed.
7. **Note a repeat.** On a non-zero exit, `Calls.repeat` looks for an earlier
   call within six hours that matches on all of: the command, the arguments,
   a digest of the full output, and `telemetry.tree_state`. The tree state is
   HEAD plus the size and mtime of dirty files. On a match, the call is noted
   ("same result as call N"), never refused.
8. **Record.** The `calls` row is started before execution, so a call that
   hangs still shows up. It is finished last, with the duration, the exit
   code, the bytes printed and in full, whether it was cached, the error, the
   fingerprint and the tree.

Stdout carries only the result. Notes and errors go to stderr, after stdout
is flushed, so a caller that pipes the output gets clean data.

`NOCK_UNRECORDED=1` turns step 8 off. The `nock-tools` check uses it when it
runs examples.

## Supervised processes

`processes.Supervisor` runs a command in its own process group:

- it streams the output to `.nock/local/logs/run-N.log` as it arrives, so the
  log survives a SIGKILL;
- it kills the group on the deadline (54) or after too long without output
  (55);
- it records a `runs` row.

The output goes through `condense.clean`:

- ANSI codes and control characters are stripped;
- of a line redrawn with `\r`, only the last version is kept;
- absolute paths are made relative;
- runs of blank lines are squeezed;
- identical consecutive lines are folded into `line (×N)`, and lines that
  differ only in digits into `(×N similar)`.

`Output.excerpt` keeps a quarter of its budget for the head and the rest for
the tail, and points at the log.

The `sh` fixture wraps the Supervisor for tools. It sets a quiet environment
(`NO_COLOR`, `TERM=dumb`, `PAGER=cat`). It then either raises, for a timeout,
a hang, a program that cannot start, or `CommandFailed` with the excerpt as
evidence, or it returns the `Output` when `check=False`.

## Telemetry

`store.Store` is one SQLite file, `.nock/local/nock.db`, with five tables:

| table | written by | when |
|---|---|---|
| `calls` | `telemetry.Calls` | every call, including refusals |
| `runs` | `processes.Supervisor` | every supervised process |
| `sessions`, `prompts`, `events` | `sessions.Sessions.ingest` | at SessionEnd, at a round's start (`catch_up`), and after a dispatched session |

Sessions are tied to calls through `CLAUDE_CODE_SESSION_ID`, which Claude Code
sets in its Bash tool and nock records on each call.

`sessions.Transcript` reads Claude Code's JSONL transcripts as best it can.
The format is internal, so a record that fits no known shape is skipped,
never a crash. Ingest replaces the session's rows, so it can be run again.
Sessions that nock dispatched carry `NOCK_ROLE`, are recorded with that role,
and are left out of the material, so reflection does not study itself.

`nock query` opens the database read-only (`mode=ro`).

Raw telemetry is never committed, because it is per-machine and does not
merge. What is shared is the reflection record (see below).

## The loop

### Hooks

`nock init` writes `.claude/settings.json` entries that call
`.nock/nock hook <NativeEvent>`. `cli.main` handles `hook` before anything
else and hands it to `hooks.run`, which:

1. reads the payload from stdin and discovers the project from its `cwd`;
2. runs nock's own handlers:
   - **SessionStart** returns the project's tools, any load failures, and,
     for a session the human started, whether a round is due;
   - **SessionEnd** ingests the transcript;
3. runs the project's `@nock.hook`s for the event, each in a fixture
   `Scope` with `payload`. Returned text is joined into `additionalContext`,
   and `Block` at `stop` becomes `{"decision": "block"}`;
4. **fails open**: any exception is printed to stderr, and the hook exits 0.
   A broken toolbox must never block the tool call that would fix it.

`setup.wired_events` wires the built-in events plus any event a project hook
listens to. The `nock-wiring` check fails when a hook exists for an event
that isn't wired.

### Reflection

`reflect.Reflection.run()`, under the `reflect` lock:

1. **Catch up.** `Sessions.catch_up` ingests any transcript written since the
   last read.
2. **Material.** `reflect.Material` renders markdown. The big picture comes
   first:
   - `requests`: the human's prompts;
   - `direction`: the commits and the areas they touch;
   - `expectations`: the last round's `expect` lines, beside each tool's
     numbers now and in the snapshot.

   Then the numbers:
   - `toolbox`: per-tool calls, failures by kind, median and longest duration,
     bytes, how often capped or cached, and retire candidates below
     ½·log₁₀(total calls);
   - `mistakes`: unknown commands;
   - `raw_work`: Bash commands grouped by `shape()`, with failures, output
     trimmed by hand, recurring pairs and the largest outputs;
   - `reading`: the files read most;
   - `sessions_table`.

   The material is saved under `.nock/local/reflections/`.
3. **Worktree.** `reflect.Worktree` creates `<root>.worktrees/reflect-NAME` on
   branch `nock/reflect-NAME`, beside the checkout. `unused_name` adds a
   suffix if a round with the same stamp already exists.
4. **Dispatch.** The brief, `briefs/reflect.md` or the project's
   `.nock/briefs/reflect.md`, is rendered through `templates.Library`.
   `!include("_framework.md")` brings in the framework guide. Then
   `dispatch.Dispatcher.launch` runs
   `claude -p --output-format stream-json --session-id … --permission-mode
   bypassPermissions` in the worktree:
   - it runs under a Supervisor, with `reflect.deadline` and
     `reflect.silence`;
   - it strips the parent session's variables;
   - it sets `NOCK_ROLE=reflector` and `NOCK_ROUND`;
   - it ingests the transcript afterwards.
5. **Record.** Inside the session, the agent calls `nock reflect record JSON`,
   which is checked against the `Record` TypedDict (summary, changes,
   verdicts, notes). It writes `.nock/reflections/STAMP.json` in the
   worktree.
6. **Land.** `Reflection.land`:
   - requires the record;
   - adds the snapshot of per-tool numbers and the window;
   - commits;
   - runs the worktree's own `.nock/nock check`, which includes `nock-tools`,
     so every example of every tool runs;
   - if the checks pass, merges `--no-ff` into `branch` under the `merge`
     lock, and removes the worktree.

   On any failure the branch and worktree stay, and the reason is printed.

`Rounds` reads `.nock/reflections/*.json`. There is one file per round, named
by timestamp, so records from different machines merge as plain files. The
next round's `expectations` section is built from the latest of them.

`Reflection.due()` is the cheap check that SessionStart runs. A round is due
after `reflect.sessions` sessions since the last round, or after
`reflect.days` days with at least `reflect.minimum` sessions.

## Extension points

Everything a project adds lives in `.nock/` and is committed:

| extension | where | registered by | used by |
|---|---|---|---|
| command (tool) | `.nock/tools/*.py` | `@nock.command` | `cli` |
| fixture | `.nock/tools/*.py` | `@nock.fixture` | `fixtures.Scope`, for any tool, check or hook |
| check | `.nock/tools/*.py` | `@nock.check` | `checks.Checks` |
| command check | `.nock/config.toml` | `[checks.NAME] command = …` (and `fix = …`) | `checks.Checks`, through the `sh` fixture |
| hook | `.nock/tools/*.py` | `@nock.hook(event)` | `hooks.HookRunner` |
| generated file | `.nock/tools/*.py` | `@nock.generator(path)` | `generators.Generators` |
| catalog | `.nock/generators/NAME/` | `nock gen new` | `generators.Catalog` |
| brief | `.nock/briefs/reflect.md` | the file's presence | `templates.Library`, before the packaged briefs |
| settings | `.nock/config.toml`, `.nock/local/config.toml` | — | `configuration.Configuration` |
| the core itself | `.nock/core/nock/` | edit it | kept by `nock upgrade`'s three-way merge |

Notes:

- **Names.** A tool's path is its function name with `_` → `-`, or `name=`,
  under `group=`. A clash with a built-in or another module is a load failure,
  not an override.
- **Fixtures** are found by parameter name. A project fixture can ask for
  other fixtures, nock's included. The names `project`, `payload` and `call`
  are supplied by the caller (`cli` or `hooks`), not by a function.
- **Checks** run concurrently, in machine-wide slots (`checks.concurrency`,
  via file locks), so parallel sessions don't pile up test suites. A check's
  verdict is 1 (the code is wrong) or 42 (the machine is not ready). The
  first look names the cause.
- **Catalogs** are directories:
  - `schema.json`: the schema of one entry;
  - `entries.json`: the entries;
  - `outputs.toml`: which template renders which file;
  - `templates/`.

  Every write re-renders the bound files. `nock-generators` fails on drift.
- **The archive.** `.nock/tools/archive/` holds retired tools. It is not
  loaded, but it is kept, so a round can bring a tool back.
- **Pinned tools** (`@nock.command(pinned=True)`) are never retire
  candidates, for tools that are rarely used but vital.

## Install and upgrade

`setup.Installer.install`:

- copies the core and writes the shim, `.gitignore` (`local/`),
  `config.toml`, `context.md` and `tools/`;
- records the source and commit in `.nock/core/VERSION.json`;
- imports `@.nock/context.md` into CLAUDE.md;
- wires `.claude/settings.json`: the hooks and the `Bash(.nock/nock:*)`
  permission.

It is idempotent, so running it again re-wires.

`setup.Upgrader.upgrade` fetches the source (a local checkout or a git URL)
and does a per-file three-way merge with `git merge-file`:

- the base is the vendored commit's files;
- ours is the project's `.nock/core`;
- theirs is upstream.

Conflicts are left marked and reported. Without a findable base, it refuses
unless `--force` is given.

## Tests and evals

- `tests/` runs the vendored `.nock/nock` in real temporary repositories,
  with real git and real processes. The reflecting agent is
  `tests/fake_agent.py`, configured as `reflect.command`, and it either adds
  a tool, stays silent or breaks the toolbox, so landing and refusing are
  both covered. There are no mocks.
- `evals/run.py` measures the whole loop with real Claude Code sessions:
  1. training tasks on a seeded shop project;
  2. one real reflection round;
  3. different test tasks of the same kinds, with and without the landed
     toolbox, each in a fresh clone.

  It reports the pass rate, cost, turns, time, tokens and tool use per kind.
  See `evals/README.md`.

The gates are `uv run pytest -n 4`, `uv run ruff check`,
`uv run ruff format` and `uv run basedpyright`. The conventions are in
`CLAUDE.md`.
