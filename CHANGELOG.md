# Changelog

All notable changes to this project are documented here. Versions follow
[PEP 440](https://peps.python.org/pep-0440/); the plugin manifests use the same version in semver
form (`0.5.0a1` is `0.5.0-alpha.1`).

## [Unreleased]

### Added

- Agent roadmap: `start` and `assign` record agents as a tree (`assign --parent/--role/--branch/--agent-model`),
  with the git branch detected automatically. `import-session` reads Claude Code session transcripts (main
  agent plus every subagent, nested by who spawned whom), Codex rollout files and `codex exec --json`
  output; `usage` records tokens by hand; `agents` prints the roadmap. Re-importing a source replaces its
  numbers. The plugin's `Stop` hook imports the stopping session into the active task.
- Dashboard: agent roadmap section (node graph of the orchestrator and its agents with curved branch
  edges, git-branch fork labels and path highlighting; each node shows role, type, branch, model, run
  time, tool calls, checks and tokens; a timeline; tokens by agent as stacked cached/fresh/output bars
  with a table view) and a Tokens tile.

### Changed

- Dashboard redesign: task header with acceptance criteria, status tiles with budget and token mini bars,
  Overview/Agents/Checks/Events tabs (keyboard navigation, URL hash deep links), an event-log filter by
  kind, and a floating replay player whose track marks each event by outcome.

### Fixed

- The identical-rerun check ignores tool caches (`__pycache__`, `.pytest_cache`, `node_modules/.cache`,
  `.coverage`, ...) that change on every run where they are not gitignored.

## [0.5.0a1] - unreleased

First public snapshot. Experimental: no token savings are demonstrated (see README, "Evidence so far").

### Added

- Plugin marketplace layout: `.claude-plugin/marketplace.json` with one plugin in
  `plugins/context-harness`, manifests for Claude Code (`.claude-plugin/plugin.json`) and Codex
  (`.codex-plugin/plugin.json`).
- MCP server configs for both hosts: `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}`; `.codex-mcp.json` uses a
  plugin-relative `cwd`, because Codex does not expand `${CLAUDE_PLUGIN_ROOT}` in MCP arguments.
- Hooks (standard-library Python, fail open, no-ops without an active task): `PreToolUse` on `Bash`
  denies test/check commands issued outside `run`; `Stop` blocks finishing while rewritten pre-existing
  tests lack a `--test-change` justification (at most 3 blocks per task). `CONTEXT_HARNESS_HOOKS=off`
  disables them, `CONTEXT_HARNESS_HOOK_TTL_HOURS` sets how long an idle task stays active (default 12).
- `context-harness-mcp` console script. The wheel carries the skill files, so `context-harness install`
  works from a pip install; MCP targets then run `python -m context_harness mcp`.
- `scripts/bundle_skill.py --check`, packaging tests (versions, manifest paths, bundle freshness) and
  GitHub Actions CI on Linux and macOS with Python 3.10–3.12.
- English version of the comparison protocol in `docs/benchmark-protocol.md`.

### Changed

- `SKILL.md`, references and helper scripts live in `src/context_harness/skill_data/`; the plugin's
  skill folder is generated from `src/` and committed.
- `run` refuses on Windows with a clear error instead of failing midway (it needs POSIX process groups).

### Included from v0.5 development

- One `start` call per task: test snapshot, memory context and run budget.
- Budgeted `run`: run countdown, stop after the full check is green plus one confirmation, repeat-failure
  stop, recorded overrides, refusal of identical runs on an unchanged tree, heartbeat for long checks.
- Shaped test output for vitest, jest, pytest, unittest, go test and cargo, with the full log on disk.
- Pre-existing test guard (`guard`, `guard --run`) and justification requirement in `report`.
- Pointer-based memory with a relevance cutoff; optional local multilingual search.
- Multi-agent coordination, independent verification, dashboard export.
- Installer for Claude Code, Codex, Cursor, Claude Desktop and Gemini CLI.

### Not included

- Benchmark scripts, raw benchmark results and run reports from development. They contain data from a
  private project.
