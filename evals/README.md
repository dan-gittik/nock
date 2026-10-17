# Does reflection pay?

`evals/run.py` measures it the only way that counts: real Claude Code
sessions, doing recurring work, with and without the toolbox a reflection
round built.

1. **The project.** `scenario/` is a small shop:
   - customers, products and orders in SQLite;
   - pricing and inventory rules;
   - feature flags kept in three places (`config/flags.json`,
     `src/shop/flags.py`, `docs/FLAGS.md`);
   - a noisy `unittest` suite.

   This checkout's nock is installed into it, with the suite configured as a
   check.
2. **Training.** Seven tasks in three kinds that recur in real work. Each is a
   real `claude -p` session on a fresh copy of the baseline, and each is
   verified:
   - **data:** questions about the data;
   - **bug:** a bug a failing suite points at;
   - **flag:** adding a flag in all three places.

   The sessions go into nock's telemetry, as they would in a real project.
3. **Reflection.** One real `nock reflect run` reads that telemetry and lands
   a toolbox, or does not.
4. **Test.** Seven *different* tasks of the same kinds. Each runs on a fresh
   clone, `--repeats` times in each arm:
   - **control:** the baseline, with an empty toolbox;
   - **toolbox:** the baseline plus what the round landed.
5. **Report.** `OUT/report.md` gives, per kind and per arm, the success rate,
   cost, turns, wall time, tokens and how often the project's tools were
   called. A per-task line follows each comparison.

```sh
uv run python evals/run.py /tmp/nock-eval --model haiku --repeats 2 --parallel 4
uv run python evals/run.py /tmp/nock-eval --skip-training --repeats 3   # more test runs, same toolbox
uv run python evals/run.py /tmp/nock-eval --report-only
```

What it does not show:

- **One round, on a small project.** Real projects drift. The claim that
  tools break loudly and get repaired needs runs across several rounds with
  the code changing in between.
- **Variance.** Sessions vary a lot from run to run, so run enough repeats
  before reading much into a difference under ~20%.
- **The test tasks are the same kinds as the training tasks.** This is by
  design, since recurring work is what tools are for. It measures reuse, not
  generality.
