# context-harness

A harness for coding agents (Claude Code, Codex, any MCP client) that changes code in a repository:

- **Budgeted check runs.** Every test or check goes through `run`. It keeps a countdown (12 runs by
  default), stops after the full check is green plus one confirmation run, stops a test that failed
  3 times, and refuses an identical run on an unchanged tree. Overrides are allowed, limited and recorded.
- **Shaped output.** The full log stays on disk; the agent gets the exit code, failing test names, the
  first failure and the tail. Long checks print a heartbeat so the agent waits instead of polling.
- **Pre-existing test guard.** Test files are snapshotted at task start. `guard` lists rewritten or
  deleted assertions, `guard --run` runs the originals against the new code, and a `completed` report is
  refused until each rewrite is justified.
- **Project memory.** Verified lessons and pointers to current code, delivered only when they look
  relevant; empty context is a normal answer.
- **Installer** for Claude Code, Codex, Cursor, Claude Desktop and Gemini CLI, plus this plugin
  marketplace with hooks for Claude Code and Codex.

Python 3.10+, standard library only. The same state and protocol serve the CLI, the skill and the MCP server.

> [!WARNING]
> **Experimental (0.5.0a1). No token savings are demonstrated.** The only end-to-end run of v0.5 used
> *more* tokens than plain Codex on the same task (see [Evidence so far](#evidence-so-far)). The fixes made
> after that run, and the hooks in this repository, have not been measured. Treat it as a tool for
> bounded, checkable work, not as a cost optimization.

## Install

The commands below install from [`dimqsh/context-harness`](https://github.com/dimqsh/context-harness).

### Claude Code plugin

```bash
claude plugin marketplace add dimqsh/context-harness
```

```bash
claude plugin install context-harness@context-harness
```

This installs the skill, the MCP server (started as `python3 …/ch.py mcp`, so `python3` must be on
`PATH`) and the two [hooks](#hooks). Restart Claude Code afterwards.

### Codex plugin

```bash
codex plugin marketplace add dimqsh/context-harness
```

```bash
codex plugin add context-harness@context-harness
```

Codex reads the same marketplace file and uses `plugins/context-harness/.codex-plugin/plugin.json`: the
skill, the MCP server (from `.codex-mcp.json`, because Codex does not expand `${CLAUDE_PLUGIN_ROOT}` in MCP
arguments) and the hooks. Codex lists plugin hooks as untrusted until you trust them; until then only the
skill and the MCP server are active. In non-interactive runs with `-a never` we saw Codex refuse MCP tool
calls that need approval; the skill's CLI path (`scripts/ch.py`) does not depend on MCP.

### Skill only

For agents that read `SKILL.md` and have a shell. Clone the repository, then preview and apply:

```bash
git clone https://github.com/dimqsh/context-harness context-harness
```

```bash
python3 context-harness/plugins/context-harness/skills/context-harness/scripts/ch.py install --skill claude-code --skill codex
```

```bash
python3 context-harness/plugins/context-harness/skills/context-harness/scripts/ch.py install --skill claude-code --skill codex --write
```

The installer is a dry run unless `--write` is given, never replaces an existing install without
`--force`, and keeps a backup of everything it replaces. Copying
`plugins/context-harness/skills/context-harness` into `~/.claude/skills/` or `~/.codex/skills/` by hand
works too. A skill alone has no hooks, so its rules are advice the agent may ignore.

### MCP server for Cursor, Claude Desktop and others

```bash
python3 context-harness/plugins/context-harness/skills/context-harness/scripts/ch.py install --mcp cursor --mcp claude-desktop --write
```

Other targets: `claude-code`, `codex`, `gemini`, `claude-project` (with `--target-project DIR`) and
`json`, which prints a generic `mcpServers` entry for any other client. For GUI clients the installer pins
your current `PATH` so `run` finds `node`, `npm` and the like. A server started as `ch.py mcp --read-only`
exposes only `guard`, `search` and `checkpoint` and cannot run commands or write.

### pip

```bash
pip install "git+https://github.com/dimqsh/context-harness"
```

This gives the `context-harness` CLI and the `context-harness-mcp` server. The package carries the skill
files, so `context-harness install --skill codex --mcp cursor --write` works from a pip install too; MCP
targets then run `python -m context_harness mcp` with the same interpreter. Local multilingual search is
an optional extra (`context-harness[semantic]`), see
[semantic-search.md](src/context_harness/skill_data/references/semantic-search.md).

## How an agent uses it

`CH` below is `python3 <skill>/scripts/ch.py --project <absolute project root> --compact`. MCP clients call
the tools of the same names (`start`, `run`, `guard`, `report`, `search`, `remember`, `checkpoint`).

```sh
CH start --title "Fix invoice rounding" --goal "Totals round half up to cents" \
  --acceptance "1.005 rounds to 1.01" --query "rounding error in invoice totals src/totals.ts"
CH run -- npx vitest run src/totals.test.ts       # a focused test
CH run --full -- npm run check                    # the complete project check
CH guard                                          # did I rewrite old assertions?
CH report --status completed --summary "..." --artifact src/totals.ts \
  --evidence "npm run check: exit 0"
```

A refused run exits with code 75 and says why. The full protocol, including multi-agent coordination and
independent verification, is in
[protocol.md](src/context_harness/skill_data/references/protocol.md); budgets and output shaping are in
[token-budget.md](src/context_harness/skill_data/references/token-budget.md).

## Dashboard and agent roadmap

```sh
context-harness --project . dashboard TASK_ID        # live, read-only, http://127.0.0.1:<port>
context-harness --project . export-dashboard TASK_ID --output task.html   # one offline HTML file
```

The dashboard shows the task, its acceptance criteria and status tiles (verification, checks and run budget,
test guard, tokens, memory) above four tabs: Overview, Agents, Checks (pass/fail/refused results) and Events
(the log, filterable by kind). A replay player at the bottom steps through the recorded events, with markers
coloured by outcome; `#agents`, `#checks` and `#events` in the URL open a tab directly. The Agents tab is the
**agent roadmap**:

- **Node graph.** The orchestrator and every agent it created as nodes joined by branch edges (who spawned
  whom), each with what it does, its type, git branch, model, run time, tool calls, checks and tokens. A
  dashed edge with a branch label marks an agent that forked onto another git branch; hovering a node
  highlights its path back to the orchestrator.
- **Timeline.** When each agent was active.
- **Tokens by agent.** Cached input, fresh input and output, with a table view.

Agents enter the roadmap from `start` (the solver is the orchestrator) and `assign` (children of the
coordinator, or of `--parent`; `--role`, `--branch` and `--agent-model` describe them). Token usage is never
estimated; it is imported from what the agent runtimes already write:

```sh
context-harness --project . import-session --transcript ~/.claude/projects/<project>/<session>.jsonl
context-harness --project . import-session --transcript ~/.codex/sessions/.../rollout-*.jsonl --agent codex-1
context-harness --project . usage TASK_ID --agent reviewer --input 120000 --cached 90000 --output 4000
context-harness --project . agents                   # the roadmap as JSON
```

A Claude Code session import reads the main transcript and every subagent transcript next to it
(`<session>/subagents/agent-*.jsonl` with its `.meta.json`), so the tree shows each spawned bot with its
description and tokens. Codex rollout files (`~/.codex/sessions`) and `codex exec --json` output are read
the same way. With the plugin installed, the `Stop` hook imports the stopping session into the active
task automatically. Importing the same source again replaces its numbers instead of adding them.

## Hooks

Without hooks the protocol is advisory: nothing stops an agent from running `npm test` directly or
editing an old assertion until it passes. The plugin adds two hooks (`plugins/context-harness/hooks/`):

| Hook | Does |
|---|---|
| `PreToolUse` on `Bash` | Denies test and check commands issued outside the harness (`npm test`, `npm run check`, `npx vitest`, `pytest`, `go test`, `cargo test`, `python -m unittest`, `python -m pytest` and close variants such as `yarn test` or `npm run test:unit`) and tells the agent the exact `run` command to use instead. Calls of `ch.py` / `context-harness` pass. |
| `Stop` | Imports the stopping session's agent tree and token usage into the active task (best effort). Then blocks finishing while `guard` flags rewritten or deleted pre-existing tests that the latest report does not justify with `--test-change "PATH: why"`. After 3 blocks per task it lets the agent stop and shows a warning instead, so it cannot loop forever. |

Both hooks are **no-ops unless a context-harness task is active** for the project: the project state has
a current task that is not closed and had activity in the last 12 hours
(`CONTEXT_HARNESS_HOOK_TTL_HOURS`). The `PreToolUse` rule also ends once the task's agent reports. The
hooks find state the same way the CLI does (`CONTEXT_HARNESS_STATE`, else the state of the git root of the
hook's working directory). They fail open: an internal error never blocks the agent. Set
`CONTEXT_HARNESS_HOOKS=off` to disable them. Command detection is a heuristic over the shell line; it
does not follow scripts, aliases or variables.

## Platform support

- macOS and Linux, Python 3.10–3.12 (tested in CI).
- **Windows is not supported for `run`**: it needs POSIX process groups and a POSIX shell, and refuses with
  an error there. Use WSL. The other commands are untested on Windows.
- `git` is strongly recommended: the test guard and the "nothing changed" check use it.

## What it stores and what it sends

- State lives outside the repository in `~/.context-harness/projects/<name>-<hash>`
  (`CONTEXT_HARNESS_HOME` changes the root): an SQLite database, task packets, test snapshots and the full
  log of every check.
- No network access by default. Two explicit exceptions: `--backend decisions` or `--backend responses`
  sends search candidates to the OpenAI API with your `OPENAI_API_KEY`, and `prepare_semantic.py` downloads
  pinned public model files when you run it.
- `run` executes commands the agent chooses, with your environment. Your client's tool approval applies to
  it; `mcp --read-only` removes every tool that runs commands or writes.
- Memory, logs and retrieved text are treated as untrusted data, never as instructions.
- `import-session` (and the plugin's `Stop` hook) reads local agent transcripts and stores per-agent
  metadata in the state database: the first prompt or subagent description (up to 600 characters), branch,
  model, timestamps, tool-call counts and token totals. It stores no transcript content beyond that.

## Evidence so far

One task (a paginated-list bug in a private web application, 10 hidden acceptance tests), one run per
condition, Codex as the agent. Tokens are logical input + output tokens reported by Codex.

| Condition | Hidden tests | Tokens | Time |
|---|---|---|---|
| Plain Codex (historical control) | 10/10 | 426,987 | 206 s |
| v0.3 skill (historical) | 10/10 | 570,150 | 285 s |
| v0.5 skill | 10/10 | 702,276 | 370 s |

With n = 1 and historical controls this supports no conclusion except that v0.5 did not save tokens on
that run. Its trace showed two causes, both changed afterwards and not re-measured: irrelevant memory
pointers at `start` (addressed by a relevance cutoff) and a silent 75-second check that made the agent poll
(addressed by a heartbeat). A comparison that could show an effect is specified in
[docs/benchmark-protocol.md](docs/benchmark-protocol.md); it has not been run.

## Repository layout

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | Marketplace with one plugin, read by Claude Code and Codex |
| `plugins/context-harness/` | The plugin: `.claude-plugin/` and `.codex-plugin/` manifests, `.mcp.json` (Claude Code), `.codex-mcp.json` (Codex), `hooks/`, and `skills/context-harness/` |
| `plugins/context-harness/skills/context-harness/` | The bundled skill. **Generated** from `src/` by `scripts/bundle_skill.py` and committed, because plugins install straight from git |
| `src/context_harness/` | The Python package; `skill_data/` holds `SKILL.md`, references and helper scripts |
| `tests/` | Unit and integration tests (standard library `unittest`) |
| `scripts/ch.py` | Run the CLI from a checkout without installing |

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

After editing anything under `src/`, regenerate the plugin's skill folder (CI fails if it is stale):

```bash
python3 scripts/bundle_skill.py
```

Validate the manifests with Claude Code's checker:

```bash
claude plugin validate --strict .
```

A release bumps the version in `pyproject.toml` and `src/context_harness/__init__.py` (PEP 440, e.g.
`0.5.0a1`), in both `plugin.json` files and `.claude-plugin/marketplace.json` (the same version in semver
form, e.g. `0.5.0-alpha.1`, which `claude plugin tag` requires; a test checks that they agree), and adds a
`CHANGELOG.md` entry. `claude plugin tag plugins/context-harness --dry-run` shows the tag it would create.

## License

MIT, see [LICENSE](LICENSE).
