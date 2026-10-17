# nock

A toolbox coding agents build for themselves: an agent-friendly CLI framework
(`commands.py`, `fixtures.py`, `render.py`, woven together in `cli.py`),
telemetry of every call and session (`store.py`, `telemetry.py`,
`sessions.py`), and reflection rounds that turn it into better tools
(`reflect.py`, `briefs/reflect.md`). It is vendored into the projects that use
it (`nock init` copies `src/nock` into their `.nock/core/`), so everything
here must run on Python 3.12 with the standard library and nothing else:
templates are `nock.templates`, schemas the JSON Schema subset in
`schema.py`.

- `uv run pytest -n 4`, `uv run ruff check`, `uv run ruff format`,
  `uv run basedpyright` — all green before a commit.
- Tests use real git repositories and real processes; the reflecting agent is
  `tests/fake_agent.py`, configured as `reflect.command`. No mocks.
- The README is the product's documentation; keep it true when behavior
  changes. `briefs/_framework.md` is the agents' documentation (`nock guide`,
  and the reflection brief): keep it true too.

## How the code is shaped

- `Project` (`project.py`) is the hub: telemetry on the main checkout
  (`project.local`), nock code — tools, settings, briefs — in the working tree
  (`project.code_path`).
- nock's own commands are tools (`builtin.py`), registered through the same
  decorators a project uses (`tools.py`); its fixtures likewise
  (`fixtures.py`).
- Exit codes are the `ExitCode` taxonomy in `errors.py`; raise the specific
  error class.
- Every byte a command prints is read by an agent: return what the next
  decision needs.

## Standards

- Classes own state and behavior; no `@staticmethod`; statuses are `StrEnum`s.
- Plain, full words; no abbreviations; long-form CLI flags only; a record is
  one JSON argument (or a file, or stdin), never JSON in a flag.
- No inline imports except a genuine cycle, marked as such.
- No abstraction a named requirement does not earn; the smallest diff that
  does the job.
- Module docstrings say what the module is in a sentence or two.
