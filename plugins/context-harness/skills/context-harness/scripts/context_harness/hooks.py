"""Agent hooks that turn two protocol rules from advice into checks, only while a task is active.

- PreToolUse (Bash): a test/check command run directly, outside `run`, is denied, so every check
  counts against the budget and the stop-after-green rule applies.
- Stop: finishing is blocked while `guard` flags rewritten or deleted pre-existing tests that the
  latest report does not justify with `--test-change`.

"Active" means: the project's state has a current task that is not closed and had activity in the
last CONTEXT_HARNESS_HOOK_TTL_HOURS (default 12), so an abandoned task cannot block later sessions.
The PreToolUse rule also ends when the task's agent reports. Without an active task both hooks
return nothing. They fail open: any internal error lets the agent continue.
Set CONTEXT_HARNESS_HOOKS=off to disable them.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TTL_HOURS = 12
MAX_STOP_BLOCKS = 3
PUNCTUATION = "();<>|&\n"
HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
WRAPPERS = {"env", "time", "nice", "nohup", "command", "exec", "builtin", "sudo", "doas"}
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "rye"}  # `<runner> run <cmd>`
KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
NPX = {"npx", "pnpx", "bunx"}
VALUE_OPTIONS = {"--prefix", "-C", "--dir", "--cwd", "-w", "--workspace", "--filter", "-F", "-p", "--package"}
TEST_SCRIPTS = re.compile(r"^(test|check)(:.*)?$")
TEST_TOOLS = {"vitest", "jest", "pytest", "py.test"}
PYTHON = re.compile(r"^(python|pypy)(\d+(\.\d+)*)?$|^py$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HARNESS = {"ch.py", "context-harness"}


def enabled():
    return os.environ.get("CONTEXT_HARNESS_HOOKS", "on").strip().lower() not in ("off", "0", "false", "no")


# --- test command detection -------------------------------------------------------------------

def _drop_heredocs(command):
    """Remove here-document bodies: their lines are data, not commands."""
    out, end = [], None
    for line in command.splitlines():
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        match = HEREDOC.search(line)
        if match:
            end = match.group(2)
    return "\n".join(out)


def _words(text):
    """Shell words; newlines and control operators outside quotes come out as punctuation tokens."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:  # unbalanced quotes: the shell rejects the line too, so nothing runs
        return []


def _segments(command, depth=0):
    """Simple commands of a shell line, as word lists (heuristic; quoting is respected)."""
    out, current = [], []
    for word in _words(_drop_heredocs(command)):
        if word and set(word) <= set(PUNCTUATION) and set(word) & set(";&|()\n"):
            if current:
                out.append(current)
            current = []
        elif word.strip():
            current.append(word)
    if current:
        out.append(current)
    if depth < 2:
        nested = []
        for words in out:
            inner = _shell_c(words)
            if inner is not None:
                nested += _segments(inner, depth + 1)
            else:
                nested.append(words)
        out = nested
    return out


def _shell_c(words):
    """The command string of `bash -c "..."` (after wrappers), else None."""
    words = _strip_wrappers(words)
    if not words or Path(words[0]).name not in SHELLS:
        return None
    for i, word in enumerate(words[1:], 1):
        if not word.startswith("-"):
            return None
        if "c" in word.lstrip("-") and not word.startswith("--") and i + 1 < len(words):
            return words[i + 1]
    return None


def _skip_options(words, i):
    while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"\d+[smhd]?", words[i])):
        i += 1
    return i


def _strip_wrappers(words):
    """Drop `VAR=value`, wrappers like `env`/`time`/`timeout 60` and `uv run`-style runners."""
    i = 0
    while i < len(words):
        word = words[i]
        name = Path(word).name
        if ASSIGNMENT.match(word) or word in KEYWORDS:
            i += 1
        elif name in WRAPPERS:
            i = _skip_options(words, i + 1)
        elif name == "timeout":
            i = _skip_options(words, i + 1)
        elif name in RUNNERS and i + 1 < len(words) and words[i + 1] == "run":
            i = _skip_options(words, i + 2)
        else:
            break
    return words[i:]


def _first_argument(words):
    """Index of the first non-option word, skipping values of known value options."""
    i = 0
    while i < len(words):
        if words[i] in VALUE_OPTIONS:
            i += 2
        elif words[i].startswith(("-", "+")):  # options and toolchains (`cargo +nightly test`)
            i += 1
        else:
            return i
    return None


def is_harness(words):
    """A context-harness invocation (`ch.py ...`, `context-harness ...`, `python -m context_harness`)."""
    if any(Path(w).name in HARNESS for w in words):
        return True
    return any(w == "-m" and nxt == "context_harness" for w, nxt in zip(words, words[1:])) or "-mcontext_harness" in words


def test_command(words):
    """True when the simple command (word list) runs a test suite or the project check."""
    words = _strip_wrappers(words)
    if not words:
        return False
    tool = Path(words[0]).name
    rest = words[1:]
    if tool in TEST_TOOLS:
        return True
    if tool in PACKAGE_MANAGERS:
        i = _first_argument(rest)
        if i is None:
            return False
        verb = rest[i]
        if verb in ("test", "t", "tst"):
            return True
        if verb in ("run", "run-script"):
            j = _first_argument(rest[i + 1:])
            return j is not None and bool(TEST_SCRIPTS.match(rest[i + 1 + j]))
        return tool in ("yarn", "pnpm", "bun") and bool(TEST_SCRIPTS.match(verb))
    if tool in NPX:
        i = _first_argument(rest)
        if i is None:
            return False
        name = rest[i]
        name = name if not name.startswith("@") else name[1:]
        return name.split("@")[0] in TEST_TOOLS
    if tool == "go":
        i = _first_argument(rest)
        return i is not None and rest[i] == "test"
    if tool == "cargo":
        i = _first_argument(rest)
        return i is not None and rest[i] in ("test", "nextest")
    if PYTHON.match(tool):
        i = 0
        while i < len(rest):
            word = rest[i]
            if word in ("-mpytest", "-munittest"):
                return True
            if word == "-m":
                return i + 1 < len(rest) and rest[i + 1] in ("pytest", "unittest")
            if word == "-c" or not word.startswith("-"):
                return False  # inline code or a script
            i += 2 if word in ("-X", "-W") else 1
    return False


def direct_test_commands(command):
    """Test/check commands in a shell line that do not go through the harness."""
    if isinstance(command, (list, tuple)):
        command = shlex.join(str(c) for c in command)
    if not isinstance(command, str) or not command.strip():
        return []
    return [" ".join(words) for words in _segments(command)
            if not is_harness(words) and test_command(words)]


# --- active task ------------------------------------------------------------------------------

def _ttl():
    try:
        return timedelta(hours=float(os.environ.get("CONTEXT_HARNESS_HOOK_TTL_HOURS", DEFAULT_TTL_HOURS)))
    except ValueError:
        return timedelta(hours=DEFAULT_TTL_HOURS)


def current_task(cwd, now=None):
    """The project's current task as a dict, or None when there is no open, recently active task."""
    from .home import default_state, project_root
    cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    env = os.environ.get("CONTEXT_HARNESS_STATE")
    state = Path(env).expanduser().resolve() if env else default_state(project_root(cwd))
    if not (state / "state.sqlite3").is_file():
        return None
    from .store import Store
    store = Store(state)
    with store.connect() as con:
        meta = {row[0]: row[1] for row in con.execute(
            "SELECT key, value FROM meta WHERE key IN ('project','current_task','current_agent')")}
        tid = meta.get("current_task")
        if not tid or not cwd.is_relative_to(Path(meta.get("project", "/nonexistent"))):
            return None
        task = con.execute("SELECT closed FROM tasks WHERE id=?", (tid,)).fetchone()
        if not task or task[0]:
            return None
        agent = meta.get("current_agent") or "solver"
        last = con.execute("SELECT MAX(created) FROM events WHERE task_id=?", (tid,)).fetchone()[0]
        report = con.execute("SELECT status, evidence FROM reports WHERE task_id=? AND agent=? "
                             "ORDER BY id DESC LIMIT 1", (tid, agent)).fetchone()
    try:
        when = datetime.fromisoformat(last) if last else None
    except ValueError:
        when = None
    if when is None or (now or datetime.now(timezone.utc)) - when > _ttl():
        return None
    return {"store": store, "task": tid, "agent": agent, "project": Path(meta["project"]),
            "report": ({"status": report[0], "evidence": json.loads(report[1])} if report else None)}


def cli_hint():
    """How the agent should call the CLI from here."""
    launcher = Path(__file__).resolve().parents[1] / "ch.py"
    return f'python3 "{launcher}"' if launcher.is_file() else "context-harness"


# --- hook handlers ----------------------------------------------------------------------------

def pre_tool_use(payload):
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    found = direct_test_commands(command)
    if not found:
        return None
    active = current_task(payload.get("cwd"))
    if not active or active["report"]:
        return None
    reason = (f"context-harness task {active['task']} is active for this project, so tests and checks go "
              f"through the harness (budget, stop-after-green rule, shaped output). Instead of "
              f"`{found[0]}` use: {cli_hint()} --project \"{active['project']}\" --compact run [--full] "
              f"[--cwd DIR] -- {found[0]}  (or the MCP `run` tool). Direct runs are allowed again after "
              f"`report` or `task-close`; CONTEXT_HARNESS_HOOKS=off disables this hook.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def unjustified_test_changes(active):
    """Paths `guard` flags that the agent's latest report does not justify."""
    from .testguard import TestGuard
    guard = TestGuard(active["store"], active["task"])
    if not guard.exists():
        return []
    _, flagged = guard.flagged()
    evidence = active["report"]["evidence"] if active["report"] else []
    changes = [line for line in evidence if isinstance(line, str) and line.startswith("harness test-change:")]
    return [path for path in flagged if not any(path in line for line in changes)]


def _stop_blocks(active, add=False):
    path = active["store"].root / "hooks" / f"{active['task']}.json"
    try:
        count = json.loads(path.read_text(encoding="utf-8")).get("stop_blocks", 0)
    except (OSError, ValueError, AttributeError):
        count = 0
    if add:
        count += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stop_blocks": count}), encoding="utf-8")
    return count


def import_session_usage(active, payload):
    """Record the agent tree and token usage of the session that is stopping (best effort)."""
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).is_file():
        return None
    from .agents import import_transcript
    try:
        return import_transcript(active["store"], active["task"], transcript, active["agent"])
    except Exception:  # noqa: BLE001 - usage is informative; never block a stop on it
        return None


def stop(payload):
    active = current_task(payload.get("cwd"))
    if not active:
        return None
    import_session_usage(active, payload)
    missing = unjustified_test_changes(active)
    if not missing:
        return None
    files = ", ".join(missing[:10]) + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
    if _stop_blocks(active) >= MAX_STOP_BLOCKS:
        return {"systemMessage": f"context-harness: pre-existing tests still rewritten without justification "
                                 f"in task {active['task']}: {files}. The Stop hook gave up after "
                                 f"{MAX_STOP_BLOCKS} blocks."}
    _stop_blocks(active, add=True)
    hint = cli_hint()
    reason = (f"context-harness task {active['task']}: pre-existing tests were rewritten or deleted without "
              f"a justification: {files}. Restore the original assertions (check them with {hint} --compact "
              f"guard --run \"<test command> {{files}}\"), or, if the old assertion is really wrong, report it: "
              f"{hint} --compact report --status completed|blocked|failed --summary S --test-change "
              f"\"PATH: why\" for each path.")
    return {"decision": "block", "reason": reason}


HANDLERS = {"pre-tool-use": pre_tool_use, "stop": stop}


def main(argv=None, stdin=None, stdout=None):
    """Entry point for hook commands: `<event>` argument, hook JSON on stdin, decision JSON on stdout."""
    argv = sys.argv[1:] if argv is None else argv
    stdout = stdout or sys.stdout
    try:
        if not enabled() or not argv or argv[0] not in HANDLERS:
            return 0
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        result = HANDLERS[argv[0]](payload if isinstance(payload, dict) else {})
    except Exception as exc:  # fail open: a hook bug must never block the agent
        if os.environ.get("CONTEXT_HARNESS_HOOK_DEBUG"):
            print(f"context-harness hook error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if result:
        stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0
