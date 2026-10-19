"""v0.5 acceptance: the MCP stdio server speaks JSON-RPC and exposes the same protocol."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


class Client:
    def __init__(self, env, *args):
        self.proc = subprocess.Popen([PY, str(ROOT / "scripts" / "ch.py"), "mcp", *args],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env)
        self.next_id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        if notify:
            return None
        return json.loads(self.proc.stdout.readline())

    def raw(self, line):
        self.proc.stdin.write(line.encode() + b"\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, name, **arguments):
        reply = self.send("tools/call", {"name": name, "arguments": arguments})
        result = reply["result"]
        return result, json.loads(result["content"][0]["text"])

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()


class Mcp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = root / "project"
        (self.project / "tests").mkdir(parents=True)
        (self.project / "tests" / "test_a.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n",
            encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.env = {**os.environ, "CONTEXT_HARNESS_HOME": str(root / "home")}

    def client(self, *args):
        c = Client(self.env, *args)
        self.addCleanup(c.close)
        return c

    def test_handshake_and_tool_list(self):
        c = self.client()
        init = c.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIn("start", init["result"]["instructions"])
        self.assertIsNone(c.send("notifications/initialized", notify=True))
        tools = {t["name"]: t for t in c.send("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"start", "run", "guard", "report", "search", "remember", "checkpoint"})
        for tool in tools.values():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertLess(len(tool["description"]), 260, "tool text is re-sent with every request")
        self.assertEqual(c.send("ping")["result"], {})
        self.assertEqual(c.send("prompts/list")["result"], {"prompts": []})

    def test_unknown_version_gets_latest(self):
        c = self.client()
        init = c.send("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")

    def test_full_task_flow(self):
        c = self.client()
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        result, data = c.call("start", project=str(self.project), title="T", goal="G", acceptance=["A"])
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["task"], data["task"])
        self.assertEqual(data["tests_snapshot"], 1)
        _, run = c.call("run", command=f'"{PY}" -m unittest discover -s tests', full=True)
        self.assertEqual(run["exit_code"], 0)
        self.assertTrue(run["budget"]["green"])
        _, guard = c.call("guard")
        self.assertTrue(guard["ok"])
        _, report = c.call("report", status="completed", summary="done", evidence=["unit tests pass"])
        self.assertEqual(report["status"], "completed")
        _, checkpoint = c.call("checkpoint")
        self.assertEqual(checkpoint["agents"][0]["status"], "completed")
        self.assertTrue(checkpoint["agents"][0]["budget"]["green"])

    def test_progress_notifications_for_long_run(self):
        env = {**self.env, "CONTEXT_HARNESS_HEARTBEAT": "0.3"}
        c = Client(env)
        self.addCleanup(c.close)
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        c.call("start", project=str(self.project), title="T", goal="G", acceptance=["A"])
        msg = {"jsonrpc": "2.0", "id": 99, "method": "tools/call",
               "params": {"name": "run", "arguments": {"command": f'"{PY}" -c "import time; time.sleep(1)"'},
                          "_meta": {"progressToken": "tok-1"}}}
        c.proc.stdin.write((json.dumps(msg) + "\n").encode())
        c.proc.stdin.flush()
        notes = []
        while True:
            line = json.loads(c.proc.stdout.readline())
            if line.get("id") == 99:
                break
            notes.append(line)
        self.assertFalse(line["result"]["isError"])
        self.assertTrue(notes)
        self.assertTrue(all(n["method"] == "notifications/progress" for n in notes))
        self.assertEqual(notes[0]["params"]["progressToken"], "tok-1")

    def test_errors_are_results_not_crashes(self):
        c = self.client()
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        result, data = c.call("run", command="echo hi")
        self.assertTrue(result["isError"])
        self.assertIn("project", data["error"])
        result, data = c.call("start", project="relative/path", title="T", goal="G", acceptance=["A"])
        self.assertTrue(result["isError"])
        unknown = c.send("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertEqual(c.raw("{not json")["error"]["code"], -32700)
        self.assertEqual(c.send("no/such/method")["error"]["code"], -32601)
        self.assertEqual(c.send("ping")["result"], {}, "server still alive")

    def test_read_only_server_hides_writers(self):
        c = self.client("--read-only")
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        names = {t["name"] for t in c.send("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"guard", "search", "checkpoint"})


if __name__ == "__main__":
    unittest.main()
