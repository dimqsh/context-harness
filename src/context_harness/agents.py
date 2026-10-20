"""Agent roadmap: who worked on a task, who spawned whom, on which branch, and the tokens they used.

The harness cannot see model usage itself, so token numbers are imported from what the agent
runtimes already write: Claude Code session transcripts (the main agent plus one transcript per
spawned subagent), Codex rollout files and `codex exec --json` output. Usage can also be recorded by
hand. Nothing here estimates tokens; a node without imported usage shows none.

Token fields are normalised to one meaning across runtimes:
- input_tokens: everything sent to the model, cached or not (Codex semantics);
- cached_input_tokens: the part served from the prompt cache;
- cache_write_tokens: input written to the cache (Claude cache creation);
- output_tokens: everything generated, reasoning included; reasoning_tokens is that subset.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_SUBAGENT_FILES = 500
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens",
                "reasoning_tokens", "requests")
SPAWN_TOOLS = frozenset({"Agent", "Task"})


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS agent_nodes(task_id TEXT NOT NULL, agent TEXT NOT NULL,
        parent TEXT, kind TEXT NOT NULL, role TEXT, agent_type TEXT, model TEXT, branch TEXT, cwd TEXT,
        started TEXT, ended TEXT, tool_calls INTEGER, source TEXT, PRIMARY KEY(task_id, agent))""")
    con.execute("""CREATE TABLE IF NOT EXISTS agent_usage(task_id TEXT NOT NULL, agent TEXT NOT NULL,
        source TEXT NOT NULL, model TEXT, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
        cache_write_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL,
        requests INTEGER NOT NULL, recorded TEXT NOT NULL, PRIMARY KEY(task_id, agent, source))""")


NODE_FIELDS = ("parent", "kind", "role", "agent_type", "model", "branch", "cwd", "started", "ended",
               "tool_calls", "source")


def upsert_node(con, tid, agent, **fields):
    """Create or update a node; fields given as None keep their stored value."""
    ensure_schema(con)
    unknown = set(fields) - set(NODE_FIELDS)
    if unknown:
        raise ValueError("Unknown agent node fields: " + ", ".join(sorted(unknown)))
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 120:
        raise ValueError("Agent name must be 1..120 characters")
    row = con.execute("SELECT * FROM agent_nodes WHERE task_id=? AND agent=?", (tid, agent)).fetchone()
    current = dict(row) if row else {"kind": "agent"}
    for key, value in fields.items():
        if value is not None:
            current[key] = value
    if isinstance(current.get("role"), str):
        current["role"] = current["role"][:600]
    con.execute("INSERT OR REPLACE INTO agent_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, agent, *(current.get(k) for k in NODE_FIELDS)))
    return current


def record_usage(con, tid, agent, source, usage, model=None):
    """Store one source's totals for an agent; importing the same source again replaces them."""
    ensure_schema(con)
    values = {}
    for key in USAGE_FIELDS:
        value = int(usage.get(key) or 0)
        if value < 0:
            raise ValueError(f"{key} must not be negative")
        values[key] = value
    if values["cached_input_tokens"] > values["input_tokens"]:
        raise ValueError("cached_input_tokens cannot exceed input_tokens (input counts cached tokens too)")
    con.execute("INSERT OR REPLACE INTO agent_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, agent, source, model, *(values[k] for k in USAGE_FIELDS), now()))
    return values


def git_branch(path):
    """Current branch of the git checkout at `path`, `detached@<sha>` when detached, else None."""
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        name = out.stdout.strip()
        if name != "HEAD":
            return name or None
        sha = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return f"detached@{sha}" if sha else None
    except (OSError, subprocess.SubprocessError):
        return None


# --- transcript parsers ---------------------------------------------------------------------------

def _empty_usage():
    return {key: 0 for key in USAGE_FIELDS}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def parse_claude_transcript(path):
    """Totals and metadata from one Claude Code JSONL transcript (main session or subagent)."""
    usage, seen, models, branches = _empty_usage(), set(), Counter(), []
    info = {"started": None, "ended": None, "cwd": None, "session_id": None, "agent_id": None,
            "prompt": None, "tool_calls": 0, "spawned": []}
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("timestamp")
            if isinstance(stamp, str):
                info["started"] = info["started"] or stamp
                info["ended"] = stamp
            info["cwd"] = entry.get("cwd") or info["cwd"]
            info["session_id"] = info["session_id"] or entry.get("sessionId")
            info["agent_id"] = info["agent_id"] or entry.get("agentId")
            if entry.get("gitBranch"):
                branches.append(entry["gitBranch"])
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            if entry.get("type") == "user" and info["prompt"] is None and not entry.get("isMeta"):
                text = _text_of(message.get("content")).strip()
                if text and not text.startswith("<"):
                    info["prompt"] = text
            if entry.get("type") != "assistant":
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    info["tool_calls"] += 1
                    if block.get("name") in SPAWN_TOOLS and block.get("id"):
                        info["spawned"].append(block["id"])
            raw = message.get("usage")
            key = message.get("id") or entry.get("requestId")
            if not isinstance(raw, dict) or (key and key in seen):
                continue  # streamed content blocks repeat the same message and usage
            if key:
                seen.add(key)
            fresh = int(raw.get("input_tokens") or 0)
            read = int(raw.get("cache_read_input_tokens") or 0)
            write = int(raw.get("cache_creation_input_tokens") or 0)
            usage["input_tokens"] += fresh + read + write
            usage["cached_input_tokens"] += read
            usage["cache_write_tokens"] += write
            usage["output_tokens"] += int(raw.get("output_tokens") or 0)
            details = raw.get("output_tokens_details") or {}
            usage["reasoning_tokens"] += int(details.get("thinking_tokens") or 0)
            usage["requests"] += 1
            if message.get("model"):
                models[message["model"]] += 1
    info["usage"] = usage
    info["model"] = models.most_common(1)[0][0] if models else None
    real = [b for b in branches if b and b != "HEAD"]
    info["branch"] = real[-1] if real else (branches[-1] if branches else None)
    return info


def parse_codex_file(path):
    """Totals from a Codex rollout file (cumulative token_count) or `codex exec --json` output."""
    usage, info = _empty_usage(), {"started": None, "ended": None, "cwd": None, "session_id": None,
                                   "model": None, "branch": None, "prompt": None, "tool_calls": 0}
    rollout_total = None
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("timestamp")
            if isinstance(stamp, str):
                info["started"] = info["started"] or stamp
                info["ended"] = stamp
            kind, payload = entry.get("type"), entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if kind == "session_meta":
                info["session_id"] = payload.get("id")
                info["cwd"] = payload.get("cwd")
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                info["branch"] = git.get("branch") or info["branch"]
            elif kind == "turn_context" and payload.get("model"):
                info["model"] = payload["model"]
            elif kind == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                rollout_total = payload["info"].get("total_token_usage") or rollout_total
            elif kind == "event_msg" and payload.get("type") == "user_message" and info["prompt"] is None:
                info["prompt"] = str(payload.get("message") or "").strip() or None
            elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
                info["tool_calls"] += 1
            elif kind == "thread.started":
                info["session_id"] = entry.get("thread_id")
            elif kind == "turn.completed" and isinstance(entry.get("usage"), dict):
                raw = entry["usage"]
                usage["input_tokens"] += int(raw.get("input_tokens") or 0)
                usage["cached_input_tokens"] += int(raw.get("cached_input_tokens") or 0)
                usage["cache_write_tokens"] += int(raw.get("cache_write_input_tokens") or 0)
                usage["output_tokens"] += int(raw.get("output_tokens") or 0)
                usage["reasoning_tokens"] += int(raw.get("reasoning_output_tokens") or 0)
                usage["requests"] += 1
            elif kind == "item.completed" and isinstance(entry.get("item"), dict):
                if entry["item"].get("type") in ("command_execution", "file_change", "mcp_tool_call"):
                    info["tool_calls"] += 1
    if rollout_total:
        usage.update({"input_tokens": int(rollout_total.get("input_tokens") or 0),
                      "cached_input_tokens": int(rollout_total.get("cached_input_tokens") or 0),
                      "cache_write_tokens": int(rollout_total.get("cache_write_input_tokens") or 0),
                      "output_tokens": int(rollout_total.get("output_tokens") or 0),
                      "reasoning_tokens": int(rollout_total.get("reasoning_output_tokens") or 0)})
    info["usage"] = usage
    return info


def detect_format(path):
    """'claude', 'codex' or None from the first records of a JSONL file."""
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for _, line in zip(range(200), handle):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in ("session_meta", "turn_context", "thread.started", "turn.completed",
                                     "event_msg", "response_item"):
                return "codex"
            if "sessionId" in entry or entry.get("type") in ("assistant", "user") and "message" in entry:
                return "claude"
    return None


def _slug(text, limit=32):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-")


# --- imports ------------------------------------------------------------------------------------

def import_claude_session(store, tid, transcript, orchestrator=None):
    """Import a Claude Code session: the main agent and every subagent it (transitively) spawned."""
    transcript = Path(transcript).expanduser().resolve(strict=True)
    store.task(tid)
    main = parse_claude_transcript(transcript)
    root = orchestrator or "orchestrator"
    source = "claude:" + (main["session_id"] or transcript.stem)
    nodes = [(root, {"kind": "orchestrator", "role": (main["prompt"] or "")[:600] or None,
                     "agent_type": "main session", "model": main["model"], "branch": main["branch"],
                     "cwd": main["cwd"], "started": main["started"], "ended": main["ended"],
                     "tool_calls": main["tool_calls"], "source": source}, main["usage"], source)]
    spawner = {tool_id: root for tool_id in main["spawned"]}
    folder = transcript.with_suffix("") / "subagents"
    subs = []
    if folder.is_dir():
        for path in sorted(folder.rglob("agent-*.jsonl"))[:MAX_SUBAGENT_FILES]:
            meta_path = path.with_name(path.stem + ".meta.json")
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            except (OSError, ValueError):
                meta = {}
            info = parse_claude_transcript(path)
            agent_id = info["agent_id"] or path.stem.removeprefix("agent-")
            agent_type = meta.get("agentType") or "subagent"
            name = f"{_slug(agent_type, 20) or 'agent'}-{agent_id[:7]}"
            subs.append((name, meta, info, agent_type, agent_id))
            for tool_id in info["spawned"]:
                spawner[tool_id] = name
    for name, meta, info, agent_type, agent_id in subs:
        parent = spawner.get(meta.get("toolUseId"), root)
        role = meta.get("description") or (info["prompt"] or "")[:200] or None
        sub_source = f"{source}:{agent_id}"
        nodes.append((name, {"parent": parent, "kind": "subagent", "role": role, "agent_type": agent_type,
                             "model": info["model"], "branch": info["branch"], "cwd": info["cwd"],
                             "started": info["started"], "ended": info["ended"],
                             "tool_calls": info["tool_calls"], "source": sub_source},
                      info["usage"], sub_source))
    return _store_import(store, tid, nodes, "claude", source)


def import_codex(store, tid, path, agent, parent=None, role=None):
    """Import one Codex run (rollout file or `codex exec --json` output) as one agent node."""
    path = Path(path).expanduser().resolve(strict=True)
    store.task(tid)
    info = parse_codex_file(path)
    source = "codex:" + (info["session_id"] or path.stem)
    fields = {"parent": parent, "kind": "agent" if parent else "orchestrator",
              "role": role or (info["prompt"] or "")[:600] or None, "agent_type": "codex",
              "model": info["model"], "branch": info["branch"], "cwd": info["cwd"],
              "started": info["started"], "ended": info["ended"], "tool_calls": info["tool_calls"],
              "source": source}
    return _store_import(store, tid, [(agent, fields, info["usage"], source)], "codex", source)


def import_transcript(store, tid, path, agent, parent=None, role=None):
    """Import whichever runtime wrote `path`."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Transcript not found: {path}")
    kind = detect_format(path)
    if kind == "claude":
        return import_claude_session(store, tid, path, orchestrator=agent)
    if kind == "codex":
        return import_codex(store, tid, path, agent, parent=parent, role=role)
    raise ValueError("Unrecognised transcript format: expected a Claude Code or Codex JSONL file")


def _store_import(store, tid, nodes, runtime, source):
    total = _empty_usage()
    with store.connect() as con:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        for name, fields, usage, usage_source in nodes:
            # A node created by start/assign keeps its kind and parent; the import adds what it observed.
            exists = con.execute("SELECT parent FROM agent_nodes WHERE task_id=? AND agent=?",
                                 (tid, name)).fetchone()
            if exists:
                fields = {**fields, "kind": None, "parent": None if exists[0] else fields.get("parent")}
            upsert_node(con, tid, name, **fields)
            if any(usage.get(k) for k in USAGE_FIELDS):
                record_usage(con, tid, name, usage_source, usage, model=fields.get("model"))
            for key in USAGE_FIELDS:
                total[key] += int(usage.get(key) or 0)
        store.emit(con, "agents_imported", {"runtime": runtime, "source": source, "agents": len(nodes),
                                            "input_tokens": total["input_tokens"],
                                            "output_tokens": total["output_tokens"]}, tid)
    return {"task": tid, "runtime": runtime, "source": source, "agents": [n[0] for n in nodes],
            "usage": total}


def record_manual_usage(store, tid, agent, usage, source="manual", model=None):
    store.task(tid)
    with store.connect() as con:
        ensure_schema(con)
        if not con.execute("SELECT 1 FROM agent_nodes WHERE task_id=? AND agent=?", (tid, agent)).fetchone():
            upsert_node(con, tid, agent, kind="agent", model=model)
        values = record_usage(con, tid, agent, source, usage, model=model)
        store.emit(con, "usage_recorded", {"agent": agent, "source": source, **values}, tid)
    return {"task": tid, "agent": agent, "source": source, "usage": values}


def roadmap(store, tid):
    """Nodes with summed usage, for the dashboard and the `agents` command. Read-only."""
    with store.connect() as con:
        try:
            nodes = [dict(r) for r in con.execute(
                "SELECT * FROM agent_nodes WHERE task_id=? ORDER BY started IS NULL, started, agent", (tid,))]
            rows = con.execute("SELECT * FROM agent_usage WHERE task_id=?", (tid,)).fetchall()
        except Exception:  # sqlite3.OperationalError on states that never recorded agents
            return {"nodes": [], "totals": _empty_usage()}
    usage = {}
    for row in rows:
        target = usage.setdefault(row["agent"], {**_empty_usage(), "sources": []})
        for key in USAGE_FIELDS:
            target[key] += row[key]
        target["sources"].append(row["source"])
    totals = _empty_usage()
    for node in nodes:
        node.pop("task_id", None)
        node["usage"] = usage.get(node["agent"])
        for key in USAGE_FIELDS:
            totals[key] += (node["usage"] or {}).get(key, 0)
    return {"nodes": nodes, "totals": totals}
