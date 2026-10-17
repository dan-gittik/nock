You are running reflection round {{ round }} on this project, in a fresh
session, in a worktree of your own: `{{ directory }}` (branch
`nock/reflect-{{ round }}`). Work only there. Nobody will answer a question:
decide, and say why in the record.

# Why this round exists

The agents working on this project — you, in other sessions — have a toolbox:
`.nock/nock`, a CLI whose commands are this project's own code, in
`.nock/tools/`. Every call to it is recorded, and so is every session: what
the human asked, every tool call the agent made, what it cost. Your job is to
read that record and change the toolbox so the work ahead takes fewer turns,
fewer tokens and fewer mistakes. You are building tools for yourself: the next
agent to need one is you, with no memory of this round.

Tools are code, not prose. A command that no longer fits the project breaks
loudly, and gets fixed or retired; a written instruction goes stale quietly
and is read differently each time. So a check beats a hook beats prose: write
commands, checks, hooks and generators — not notes, not skills, not
instructions.

# The big picture first

Before the numbers, understand the work — the material below starts with it:

1. **What the human asked for**, in their words. What is the project doing?
   What do they keep asking for? What did they have to correct, or ask twice?
   A correction is the strongest signal there is: an agent got something wrong
   that a check could have caught, or lacked what a tool could have told it.
2. **Where the code is going**: which areas grow, which are rewritten, which
   are dropped. Build for where the project is going, not where it was, and
   retire tools for what is gone.
3. **The toolbox as a whole**: read every module in `.nock/tools/` and run
   each tool once. Does it still fit the code? Do tools overlap? Are names and
   arguments consistent across them? Is something missing that the work
   plainly needs?

Then let the numbers find and confirm candidates. Drill down with
`.nock/nock query` — read the actual events of the sessions involved before
you decide anything.

# What to change

- **Add a command** when the same work recurs across sessions: the same shell
  commands in sequence, the same files read to answer the same question, the
  same output trimmed by hand, the same failure retried. Once is not a
  pattern: build for what happened at least twice, in different sessions. A
  good tool
  - does a whole job that took several turns, and returns only what the next
    decision needs — the failures, not the log; a count when a count is enough;
  - is flat — one function, no tools calling tools;
  - beats plain shell, measurably: if you cannot say how many turns or bytes
    it saves, do not build it. A wrapper that only renames a command saves
    nothing and costs a line in every session's context.
- **Change a tool** that is called wrong (usage errors: fix its names,
  defaults, signature or docstring — the commands agents tried that do not
  exist often name what they expected), that breaks (machine errors), that
  prints too much (capped, high mean_bytes: return less), or that is slow
  (`cache=`, or narrow it).
- **Retire a tool** nobody uses, that the project outgrew, or that another
  subsumes: `git mv` it into `.nock/tools/archive/`, which is not loaded and
  can be restored. Pinned tools stay.
- **Add a check** when the human corrected the same kind of mistake twice, or
  a rule they keep restating can be tested mechanically.
- **Add a generator** when a file is derived from data and keeps drifting.
- **Change nothing** that nothing earns. A round that confirms the toolbox is
  a good round; fewer, better tools beat more tools.

Change only `.nock/` — this round changes the toolbox, not the product. The
one exception is a file a generator you add writes: run `.nock/nock gen write`
and commit it with the generator. A new check must pass on the code as it
stands: ask for `changed` to hold only new code to it, and name the existing
violations in the record's notes.

# How to write a tool

!include("_framework.md")

# Verify

Run every tool you added or changed, on this project, as an agent would, and
read the output as that agent: is it what the next decision needs, and
nothing more? Give each new command `examples=[...]`. Then run
`.nock/nock check` — the round lands only when it passes.

# Finish

1. Commit on this branch — a commit per change, its message saying why.
2. Record the round. It is what the next round holds you to:

       .nock/nock reflect record '{"summary": "a sentence or two",
         "changes": [{"kind": "add", "target": "failures",
           "why": "the evidence, with numbers",
           "expect": "what should measurably change by the next round, and how to tell"}],
         "verdicts": [{"expectation": "one the last round left", "held": true,
           "evidence": "the numbers"}],
         "notes": "what the next round should look at"}'

   `kind` is add, change, retire, check, generator or other. Judge every
   expectation the last round left: held, did not (then fix or revert that
   change), or too early to tell (`"held": null`).
3. End. nock runs the checks and lands the branch.

# The material

{{ material }}
