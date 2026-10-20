"""Read-only local dashboard of recorded task events, with portable replay export."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def build_snapshot(store, task):
    """Return task-scoped events and current metadata, without scanning source files."""
    snapshot = store.summary(task)
    with store.connect() as con:
        counts = {row[0]: row[1] for row in con.execute(
            "SELECT status,COUNT(*) FROM memories GROUP BY status")}
        try:  # Read-only: the budgets table exists only once a run budget was set.
            budgets = {row["agent"]: {k: row[k] for k in ("max_runs", "max_after_green",
                                                          "max_fail_streak", "max_overrides")}
                       for row in con.execute("SELECT * FROM budgets WHERE task_id=?", (task,))}
        except sqlite3.OperationalError:
            budgets = {}
    from .agents import roadmap
    snapshot.update(
        budgets=budgets,
        roadmap=roadmap(store, task),
        events=store.events(task),
        memory_stats={"total": sum(counts.values()), "verified": counts.get("verified", 0),
                      "proposed": counts.get("proposed", 0)},
        project=str(store.project), state=str(store.root),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    return snapshot


def render_html(snapshot, live=False):
    """Embed untrusted journal data as JSON, never as executable markup."""
    payload = json.dumps({"snapshot": snapshot, "live": bool(live)}, ensure_ascii=False)
    for char, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                          ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        payload = payload.replace(char, escaped)
    template = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    return template.replace("__CONTEXT_HARNESS_DATA__", payload)


def export_dashboard(store, task, output):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(store, task)
    output.write_text(render_html(snapshot), encoding="utf-8")
    return {"output": str(output), "task": task, "events": len(snapshot["events"]),
            "mode": "recorded replay", "live": False}


def make_server(store, task, port=0):
    """Bind loopback only. Expose two read-only routes; never serve filesystem paths."""
    store.task(task)  # Fail before binding if the requested task does not exist.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Local binding alone does not prevent a browser from reaching this
            # process through an attacker-controlled DNS name.
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host", "").lower() not in hosts:
                self.send_error(403, "Loopback host required")
                return
            if self.path not in ("/", "/api/snapshot"):
                self.send_error(404, "Not found")
                return
            try:
                snapshot = build_snapshot(store, task)
                if self.path == "/":
                    body = render_html(snapshot, live=True).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                else:
                    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
            except (ValueError, OSError):
                self.send_error(503, "State unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, task, port=8765):
    server = make_server(store, task, port)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
