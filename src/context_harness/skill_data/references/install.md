# Installing for several agents

The harness ships several ways; all use the same state and protocol. The Claude Code and Codex
plugins (`claude plugin install` / `codex plugin add`, see the repository README) bundle this skill, the MCP
server and two hooks; the installer below covers everything else.

| Form | For | Entry point |
|---|---|---|
| Skill | Agents with a shell that read `SKILL.md` (Codex, Claude Code) | `scripts/ch.py` |
| MCP server | Any MCP client (Claude Code, Codex, Cursor, Claude Desktop, Gemini CLI, ...) | `scripts/ch.py mcp` |
| CLI | Scripts, CI, coordinators | `scripts/ch.py` or `context-harness` after `pip install` |
| Plugin | Claude Code, Codex | skill + MCP server + hooks (`hooks/hooks.json`) |

The installer is a dry run unless `--write` is given. It never overwrites an existing install or a different server entry without `--force`, and it keeps a timestamped backup of every config file (next to it) and skill folder (in `~/.context-harness/backups/skills/`, never inside a skills folder, where an agent would load it as a second skill) it replaces.

```sh
python3 scripts/ch.py install --skill claude-code --skill codex --mcp claude-code --mcp codex --mcp cursor
python3 scripts/ch.py install --skill claude-code --skill codex --mcp claude-code --mcp codex --mcp cursor --write
```

Skill targets: `claude-code` (`~/.claude/skills/context-harness`), `codex` (`$CODEX_HOME` or `~/.codex/skills/...`), `claude-project` (`<--target-project>/.claude/skills/...`), or an absolute folder.

MCP targets:

| Target | What changes |
|---|---|
| `claude-code` | Runs `claude mcp add --scope user context-harness -- <python> <ch.py> mcp` |
| `claude-project` | `<--target-project>/.mcp.json` |
| `codex` | `[mcp_servers.context-harness]` in `~/.codex/config.toml` |
| `cursor` | `~/.cursor/mcp.json` (pins the current `PATH` so `run` finds node/npm) |
| `claude-desktop` | `claude_desktop_config.json` (pins `PATH` too) |
| `gemini` | `~/.gemini/settings.json` |
| `json` | Prints a generic `mcpServers` entry for any other client |

The MCP server runs from the first skill folder installed in the same command, otherwise from this bundled skill folder; after `pip install` it runs as `python -m context_harness mcp` with that interpreter (also available as `context-harness-mcp`). Use `--python /path/to/venv/bin/python` when hybrid retrieval needs the semantic extras.

## State

Without `--state`, state lives in `~/.context-harness/projects/<name>-<hash>` (override with `CONTEXT_HARNESS_HOME`), outside the repository. One MCP registration therefore serves every project: the first `start` call names the project and later calls reuse it. A server started with `--state DIR` is bound to that one state. `mcp --read-only` exposes only `guard`, `search` and `checkpoint`.

## Shell for `run`

`run` executes through `/bin/bash -c` in the project folder with the server's environment. It needs macOS, Linux or WSL; on Windows it refuses with an error. GUI clients start servers without your shell profile; the installer pins `PATH` for them. Override with `CONTEXT_HARNESS_SHELL="/bin/zsh -lc"` if checks need your login profile.

`run` executes commands chosen by the agent, so your MCP client's approval prompt for tool calls applies to it. Use `--read-only` where command execution is not wanted.
