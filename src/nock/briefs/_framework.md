A tool is a decorated function in a module under `.nock/tools/` (any name; a
file per area is good). It is live on the next call — nothing to register.

    import nock

    @nock.command(cache="src/** tests/**", examples=["--path tests/unit"])
    def failures(sh, *, path: str = "tests") -> list[dict]:
        """The failing tests, with the first line of each error."""
        run = sh(f"python -m pytest -q -rf {path}", check=False)
        return [{"test": test, "error": error} for test, error in parse(run.lines)]

`.nock/nock failures --path tests/unit` runs it.

**The signature is the command line.** A positional parameter is an argument;
a keyword-only one (after `*`) is an `--option` — a `bool` is a flag, a
`list[str]` repeats, an `Enum` or `Literal` gives the choices. A parameter
typed as a TypedDict or a dataclass is one JSON argument (inline, a path to a
JSON file, or stdin), checked against the type. The docstring's first line is
the tool's summary everywhere it is listed; the rest is its `--help`.
`group="db"` makes it `.nock/nock db NAME`.

**Ask for what you need by name**, as pytest fixtures are asked for:
`project`; `root` (the working tree, a Path); `git` (`git("log", "-1")`
returns the output); `sh` (runs a command supervised, logged and condensed:
`sh(command)` ends the call on failure, `sh(command, check=False)` returns an
Output with `.code`, `.ok`, `.lines`, `.text`, `.raw` and `.log`; it takes
`deadline=`, `silence=`, `directory=`, `input=`); `tmp` (a directory removed
after the call); `log` (notes to the call's log file — never to the caller);
`changed` (files changed against the main branch); `config`. Make your own
with `@nock.fixture`, and `yield` to clean up after the call:

    @nock.fixture
    def db(root):
        connection = sqlite3.connect(root / "app.db")
        yield connection
        connection.close()

**Return data; nock prints it**, as compactly as its shape allows: None
prints nothing (the exit code says it); a string as is; a dict as
`key: value` lines; a list one item per line; a list of dicts as CSV under a
header; nested data as indented `key: value`. Returning an Output from `sh`
passes the command's condensed output and exit code through. Long output is
capped and the rest saved to a file whose path is printed; `--json` gives any
caller JSON. Every byte a tool prints is read by an agent: return what the
next decision needs, not everything you have.

**Fail by raising** — it sets the exit code: `nock.Failed("why", evidence)`
is 1, the answer is no (the evidence prints like a return value);
`nock.UsageError` is 40, called wrong; `nock.NotFoundError` is 44;
`nock.PreconditionError` is 42, the machine or the state is not ready.
Returning `False` is a plain no. A crash prints the tool's own lines with the
values they held, and is recorded.

**Checks** return their problems — strings, or `nock.Problem(message, path,
line)` — and nothing when all is well. `.nock/nock check` runs them all.

    @nock.check
    def migrations_reversible(root, changed):
        """Every new migration has a down step."""
        return [
            nock.Problem("no down()", path)
            for path in changed
            if path.startswith("migrations/") and "def down" not in (root / path).read_text()
        ]

Asking for `changed` keeps a check to new code, so it can land while old code
still breaks the rule. A plain command can be a check too, in
`.nock/config.toml`: `[checks.test]` with `command = "pytest -q"`.

**Hooks** react to Claude Code's events: `@nock.hook("session-start")` (or
`prompt`, `after-tool`, `tool-failed`, `stop`, `session-end`), asking for
`payload`, the event's JSON. Text a hook returns is added to what the agent
reads; `raise nock.Block("why")` at `stop` refuses the stop. A hook runs on
every such event of every session — keep it fast, and prefer a check.

**Generators** keep derived files in step with their source:
`@nock.generator("src/errors.py")` returns the file's text; `.nock/nock gen`
writes it, and `.nock/nock check` fails when the file drifts. For a list the
code is generated from (error codes, flags, strings), see
`.nock/nock gen new --help`.

**Caching**: `cache="src/** tests/**"` replays the last result while those
files and the tool's own code are unchanged; `--fresh` runs it anyway.

**Examples**: `examples=["--path tests/unit"]` shows in `--help`, and
`.nock/nock check` runs each one — a tool that breaks fails the gate.

Tools are standard-library Python, run by the vendored nock on `python3`.
`.nock/nock calls --tool NAME` shows how a tool has been called and how it
ended.
