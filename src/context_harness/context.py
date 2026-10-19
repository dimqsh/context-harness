"""Progressive disclosure with explicit UTF-8 budgets, never a text rewriter."""
from __future__ import annotations

import json
from pathlib import Path
import re


def json_bytes(value):
    """Match the CLI and packet serializer, including its trailing newline."""
    return len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def require_budget(value, budget_bytes):
    if not isinstance(budget_bytes, int) or not 512 <= budget_bytes <= 1_000_000:
        raise ValueError("Context budget must be 512..1000000 UTF-8 bytes")
    if json_bytes(value) > budget_bytes:
        raise ValueError("Required context exceeds byte budget; increase it, do not truncate goals or evidence")
    return value


def memory_index(record):
    return {key: record[key] for key in (
        "id", "title", "source", "domain", "revision", "source_hash"
    ) if key in record}


def page_records(base, records, *, key="records", budget_bytes=6000, offset=0):
    """A prefix page of complete records. No claim/identifier is abbreviated."""
    if not isinstance(offset, int) or not 0 <= offset <= len(records):
        raise ValueError("Offset outside this result set")
    value = {**base, key: [], "total": len(records), "returned": 0,
             "next_offset": offset if offset < len(records) else None,
             "budget_bytes": budget_bytes, "budget_unit": "UTF-8 bytes, not model tokens"}
    require_budget(value, budget_bytes)
    for pos, record in enumerate(records[offset:], offset):
        candidate = {**value, key: [*value[key], record], "returned": value["returned"] + 1,
                     "next_offset": pos + 1 if pos + 1 < len(records) else None}
        if json_bytes(candidate) > budget_bytes:
            if not value[key]:
                raise ValueError("One complete record exceeds byte budget; increase it")
            break
        value = candidate
    return value


_LINES = re.compile(r"\blines (\d+)-(\d+)\b")
_SOURCE = re.compile(r"^(?P<path>.*?)(?::(?P<line>\d+))?$")
MAX_INLINE_LINES = 60


def memory_view(project, record, mode="pointers"):
    """One record as the agent should see it, read through to the current file.

    Code chunks (record text still present in its source file) become a pointer, or the
    current text of that range in `inline` mode: the agent never reads the same bytes twice.
    Facts and lessons are not in the repository, so they are always delivered in full.
    """
    m = _SOURCE.match(record.get("source", ""))
    rel = m.group("path") if m else ""
    path = Path(rel) if rel else None
    if path is not None and not path.is_absolute():
        path = Path(project) / path
    chunk = None
    if path is not None:
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(Path(project).resolve()) and resolved.is_file():
                text = resolved.read_text(encoding="utf-8", errors="replace")
                body = record.get("content", "").strip()
                if body and body[:400] in text:
                    chunk = (resolved, text)
        except OSError:
            chunk = None
    if chunk is None:
        out = {"id": record["id"], "kind": "fact", "title": record["title"], "content": record["content"],
               "source": record["source"]}
        if record.get("evidence"):
            out["evidence"] = record["evidence"]
        return out
    resolved, text = chunk
    lines = text.splitlines()
    span = _LINES.search(record.get("title", ""))
    if span:
        start, end = int(span.group(1)), int(span.group(2))
    else:
        offset = text.find(record["content"].strip()[:400])
        start = text.count("\n", 0, offset) + 1
        end = start + record["content"].strip().count("\n")
    start, end = max(1, start), min(len(lines), max(start, end))
    out = {"id": record["id"], "kind": "code", "path": resolved.relative_to(Path(project).resolve()).as_posix(),
           "lines": f"{start}-{end}"}
    symbols = record.get("title", "").split(" | ")
    if len(symbols) >= 3 and symbols[-1].strip():
        out["symbols"] = symbols[-1].strip()
    if mode == "inline":
        last = min(end, start + MAX_INLINE_LINES - 1)
        out["text"] = "\n".join(lines[start - 1:last])
        if last < end:
            out["truncated_after_line"] = last
    return out


_IDENT = re.compile(r"[A-Za-z_$][\w$]*(?:[-./][\w$]+)+|[A-Za-z_$][\w$]*_[\w$]+|[a-z][a-z0-9]*[A-Z][\w$]*")
_WORD = re.compile(r"[^\W_]+")
STOPWORDS = frozenset("""
the and for with that this from into when then than only also each list item items value values data
file files code test tests should must does make made keep kept work works page pages using used none
error errors issue issues local update updates fix fixed change changes new old get set add remove
это как что при для или без его она они все так уже ещё еще если надо нужно чтобы когда
""".split())


def query_identifiers(text):
    """File and identifier names in a query: hyphen/dot/slash names, snake_case and camelCase."""
    return list(dict.fromkeys(m for m in _IDENT.findall(text) if len(m) >= 4))


def _words(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w.casefold() for w in _WORD.findall(text)]


def relevant_records(store, query, records, max_share=0.02, min_coverage=0.5):
    """Keep code pointers only when retrieval is confident; otherwise the agent searches itself.

    A wrong pointer sends the agent to read unrelated files. Identifier matches win outright.
    Otherwise a code record must cover at least `min_coverage` of the query's rare-word weight
    (IDF over memory, rare = in at most `max_share` of verified records). Facts/lessons are
    always kept: they are few and are
    matched by meaning, not by shared code words.
    """
    import math
    named = [r for r in records if r.get("identifier_match")]
    is_fact = lambda r: memory_view(store.project, r).get("kind") == "fact"
    if named:
        kept = [r for r in records if r.get("identifier_match") or is_fact(r)]
        return kept, len(records) - len(kept), "identifier named in the query"
    words = [w for w in dict.fromkeys(_words(query)) if len(w) >= 4 and w not in STOPWORDS]
    weights = {}
    with store.connect() as con:
        total = con.execute("SELECT COUNT(*) FROM memories WHERE status='verified'").fetchone()[0] or 1
        for w in words[:24]:
            df = con.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
                             ('"' + w.replace('"', '""') + '"',)).fetchone()[0]
            if 0 < df <= max(3, max_share * total):
                weights[w] = math.log(total / df)
    mass = sum(weights.values())
    kept = []
    for r in records:
        if is_fact(r):
            kept.append(r)
            continue
        if not weights:
            continue
        haystack = set(_words(" ".join((r.get("source", ""), r.get("title", ""), r.get("content", "")))))
        hits = [w for w in weights if w in haystack]
        if hits and sum(weights[w] for w in hits) >= min_coverage * mass:
            kept.append(r)
    if not weights:
        reason = "no rare query word in memory; code pointers dropped (name files/identifiers, in English)"
    else:
        reason = f"code pointers must cover >= {min_coverage:.0%} of rare query words ({', '.join(list(weights)[:8])})"
    return kept, len(records) - len(kept), reason


def merge_pointers(rows):
    """One pointer per file: several chunks of the same file become one line-range list."""
    merged, by_path = [], {}
    for row in rows:
        if row.get("kind") != "code":
            merged.append(row)
            continue
        if row["path"] not in by_path:
            by_path[row["path"]] = row = {**row, "ids": [row["id"]]}
            row.pop("id")
            merged.append(row)
            continue
        first = by_path[row["path"]]
        first["ids"].append(row["id"])
        spans = [tuple(map(int, s.split("-"))) for s in (first["lines"] + "," + row["lines"]).split(",")]
        spans.sort()
        joined = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= joined[-1][1] + 1:
                joined[-1][1] = max(joined[-1][1], b)
            else:
                joined.append([a, b])
        first["lines"] = ",".join(f"{a}-{b}" for a, b in joined)
        if row.get("symbols"):
            first["symbols"] = ", ".join(dict.fromkeys(
                (first.get("symbols", "") + ", " + row["symbols"]).strip(", ").split(", ")))
        if "text" in row:
            first["text"] = first.get("text", "") + "\n" + row["text"]
    for row in merged:
        if row.get("kind") == "code" and len(row["ids"]) == 1:
            row["id"] = row.pop("ids")[0]
    return merged


def search_view(result, *, view="index", budget_bytes=6000, offset=0, project=None):
    if view not in ("index", "full", "pointers", "inline"):
        raise ValueError("Context view must be pointers, inline, index or full")
    if view in ("pointers", "inline"):
        if project is None:
            raise ValueError("Pointer and inline views need the project root")
        base = {key: result[key] for key in (
            "backend", "retrieval", "retrieval_note", "query_variants", "elapsed_ms", "shortlist_size"
        ) if key in result}
        base.update({"view": view, "untrusted_data": True})
        return page_records(base, [memory_view(project, r, view) for r in result["records"]],
                            budget_bytes=budget_bytes, offset=offset)
    base = {key: result[key] for key in (
        "backend", "model", "api_called", "usage", "retrieval", "retrieval_model",
        "query_variants", "elapsed_ms", "shortlist_size"
    ) if key in result}
    base.update({"view": view, "untrusted_data": True,
                 "detail_command": "memory-read ID [ID ...]"})
    records = result["records"]
    if view == "index":
        records = [memory_index(r) for r in records]
    return page_records(base, records, budget_bytes=budget_bytes, offset=offset)
