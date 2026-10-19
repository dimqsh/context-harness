# v0.5 comparison protocol (not run yet)

Goal: find out whether v0.5 lowers the total cost of a session without losing quality. Single runs of
v0.3/v0.4 cannot show this: a difference of 33 vs 35 passing hidden tests out of 37 came down to one
decision of the agent.

## Conditions

| Condition | What the agent gets |
|---|---|
| A. Plain agent | Only the task |
| B. v0.5 skill | Task + v0.5 SKILL.md (CLI) |
| C. v0.5 MCP | Task + the MCP server, no SKILL.md |
| D. Ablations (if possible) | B without budgets (`--max-runs 200 --max-after-green 200 --max-fail-streak 200`); B without memory (empty state); B without the guard (`--no-test-snapshot`) |

One model, one reasoning effort, one agent CLI version, one repository revision, a separate copy per run.
Controls are run again, not taken from history.

## Tasks

At least 3 tasks of different kinds in one repository, with hidden acceptance tests frozen before the
runs: (1) a short bug fix, (2) a complex cross-module task, (3) a task for which a verified lesson exists
in memory. For (3) the lesson is added before the runs and identically for B and C; A cannot see it by
definition, which is exactly the memory benefit being tested.

## Repetitions and order

- At least 5 repetitions per condition and task, in pairs: within each pair the conditions run in random
  order, one right after the other.
- The provider cache is not controlled, so the order is randomized rather than fixed.

## Metrics (per run)

- Quality: frozen tests passed; pre-existing (regression) and new tests counted separately.
- Logical tokens: input + output from `turn.completed.usage`; uncached input and output separately.
- Number of model calls (trace items), number of check runs, number of full checks.
- Wall-clock time.
- For B/C: `run` refusals, overrides, `guard` flags, `--test-change` entries used.

## Analysis

- Medians and interquartile ranges per condition; bootstrap 95% CI of the difference of medians
  (10,000 resamples).
- Pairs: Wilcoxon signed-rank for tokens and time; McNemar for "fully accepted or not".
- "v0.5 is better" only if the CI of the token difference excludes 0 and quality is not worse (the lower
  CI bound of the difference in passed tests is at least 0, or McNemar shows no difference).
- Publish every run, including failed and interrupted ones; never drop "outliers" after seeing results.

## Separate contributions

The ablations in D answer which part has an effect: budgets and the stop rule, memory, or the guard.
Without them a change in total tokens cannot be attributed to a specific feature.
