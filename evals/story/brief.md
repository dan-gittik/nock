# A month at a small software company

Write the engineering record of one month at a small software company: the
system it runs, and everything that happened to that system over four weeks.

The record will be used to rebuild the system as a small working model that
runs on one machine, and to replay the month on it. So it has to be precise
enough to build from. Every problem must be reproducible, every cause must be
findable in the system itself, and every resolution must be verifiable by
someone who wasn't there.

Write it as the team's own record would read, not as fiction about a team. It
should be plain, specific and a little untidy, the way real systems and real
teams are.

## The company and the system

Choose the business. It must involve:
- money: orders, invoices, payouts or refunds;
- at least two outside partners the system talks to over the network;
- work that happens on a schedule.

Logistics, bookings, subscriptions and marketplaces all fit. Keep the team
small (four to six people) and give them names, roles and habits. The people
who send reports during the month include, among others:
- a support lead;
- someone in finance;
- a founder;
- a partner's integration contact;
- the monitoring system's alerts.

The system has this shape. Name its parts as the team would.

- **An HTTP API** that customers and the internal dashboard use, with
  authentication, a few dozen endpoints, and request logging.
- **A worker** that processes jobs from a queue: assignments, notifications,
  calls to partners.
- **Scheduled jobs:** a nightly billing or settlement run, and one or two
  others.
- **Partner integrations:** outbound calls, inbound webhooks, signatures or
  keys, retries, and rate limits. Partners are imperfect. They time out,
  change things with little notice, and send duplicates.
- **A relational database,** with migrations and two environments, staging and
  production. A read replica is optional. Connection settings live where they
  realistically would: environment files, a config module and defaults, not
  quite consistent with each other.
- **Logs:** each component writes its own, and their formats differ a little.
  Some are structured and some are plain lines. They rotate. There is also a
  simple metrics or health output.
- **A test suite** that is decent but not complete, and has one test known to
  be flaky.
- **The repository's other contents:**
  - a README;
  - a Makefile or scripts directory;
  - a runbook that is partly out of date;
  - a changelog;
  - a git history going back about a year, with realistic commit messages.

Constraints for the model:
- Python and SQLite only (one database file per environment);
- partners as small local services;
- time driven by a clock the model can control;
- about 5,000–8,000 lines of application code in total.

Choose details that make it feel lived-in: a deprecated endpoint still in use,
a config flag nobody remembers, a module two people wrote differently.

## The month

The month has four weeks, with five reports a week, twenty in all. A report is
anything that lands on the team and needs someone to work on the system:

- **Something broke:** an alert, a customer complaint, a partner reporting
  failures.
- **Something is slow or degraded.**
- **A question about the data:** finance, the founder or support needs a
  number or a list, and getting it right requires knowing the system's
  semantics (what counts as revenue, which status means what).
- **Data that needs repair:** wrong rows that something produced. The repair
  has to find what is affected, fix the cause, and correct the data.
- **A feature that requires investigation first:** the change is not
  mechanical. The person doing it has to understand how a part of the system
  works before they can change it.
- **Operational work:** rotating a key, running a migration through both
  environments, handling a partner's announced change.

Make the work **recur the way real work does**:
- Most reports belong to a few families of trouble that keep coming back, each
  time with a *different* cause. For example, three "partner calls failing"
  reports over the month might be an expired key, a changed payload, and a
  rate limit.
- Each family should appear at least twice in weeks 1–3, and again in week 4.
- A few reports (three or four in the month) should be genuine one-offs that
  resemble nothing else.
- Week 4 should feel like any other week, not like a finale.

Each report must be **independent within its week**:
- It starts from the system as it stands at the beginning of that week, plus
  whatever went wrong for this report alone.
- It must not depend on how another report that week was handled.

Between weeks, the team's resolutions land, so they are part of the system
from the next week on.

**Between weeks the system also changes for reasons of its own**: two or three
changes by teammates each week, unrelated to the reports. For example:
- a refactor that moves or splits a module;
- a log format that changes;
- a renamed table or column;
- a dependency replaced;
- a new environment variable.

Ordinary engineering. Some of these should quietly make earlier knowledge
wrong: the place a thing was found last week is not where it is now.

## For each report, write

1. **ID and week:** for example `W2-3`.
2. **The family** it belongs to, or "one-off".
3. **The message, verbatim,** as its sender wrote it: their channel, their
   tone, their level of detail. Report symptoms as the sender saw them, and
   never the cause. Senders are sometimes vague, sometimes wrong about what's
   happening, and sometimes include a misleading detail.
4. **What actually happened:** the cause, precisely. Name the files, the rows,
   the config values, the commit that introduced it, and the sequence of
   events.
5. **How it shows up in the system:**
   - what the logs contain, with example lines and rough volume;
   - what the data looks like;
   - which tests fail, if any;
   - what a request returns;
   - which other symptoms are present but irrelevant to this report, since
     real systems always have some.
6. **How it was set up:** what about the system had to be different for this
   to happen, expressed as concrete changes to code, config, data or partner
   behavior, on top of the week's starting state. If it arrived as a commit,
   give that commit's message as the teammate wrote it.
7. **The resolution:** what the team did, and what a good resolution looks
   like. Include acceptable alternatives where several fixes are reasonable.
8. **How we know it's resolved,** in terms someone else can check without
   judgment:
   - a request that now succeeds;
   - a query whose result is now right;
   - a test that passes;
   - the exact number or list the sender needed;
   - rows that must now be correct;
   - logs that no longer show an error under normal traffic.

   Also list what a careless resolution would break or get wrong.
9. **Severity, and the expected effort** for a competent engineer new to this
   part of the system.

## For each week, also write

- The week's teammate changes: each one's commit message, what it changed, and
  which later reports it affects, if any.
- What normal traffic looks like that week: volumes, the customer mix, partner
  behavior. The system will be run under this traffic to produce its logs.

## Format

One Markdown document, `STORY.md`, in this order:

1. The company and the team.
2. The system: components, data model (tables and key columns), config and
   environments, logs, partners, jobs, tests, repository layout, and the
   history that led here.
3. Weeks 1–4: for each week, its changes, its traffic, then its five reports,
   each with the fields above under the same headings, every time.
4. An index of reports by family.

Be concrete everywhere: real names, numbers, timestamps, file paths and
example log lines. Where you have to choose, choose what a real team would
have done, including the slightly wrong things real teams do.
