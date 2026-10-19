"""Deterministic test/check output digests. The full log always stays on disk.

Short output passes through unchanged: a digest of a short log costs more than the log.
Long output becomes counts, failing test names, the first failure excerpt and a tail.
Nothing here decides pass/fail; the real exit code is always reported next to it.
"""
from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
PASSTHROUGH_BYTES = 1500
MAX_FAILED_NAMES = 12
EXCERPT_LINES = 30
TAIL_LINES = 8

# Summary lines of common runners; each yields (passed, failed, skipped, total) where known.
_COUNT_PATTERNS = (
    # vitest: "Tests  2 failed | 27 passed (29)"
    ("vitest", re.compile(r"^\s*Tests\s+(?P<body>.*?)\s*\((?P<total>\d+)\)\s*$")),
    # jest: "Tests:       1 failed, 5 passed, 6 total"
    ("jest", re.compile(r"^\s*Tests:\s+(?P<body>.*?)(?P<total>\d+) total\s*$")),
    # pytest: "==== 1 failed, 3 passed in 0.12s ===="
    ("pytest", re.compile(r"^=+ (?P<body>.*?\b(?:passed|failed|error|errors)\b.*?) in [\d.]+s(?: \([^)]*\))? =+$")),
    # cargo: "test result: FAILED. 3 passed; 1 failed; 0 ignored"
    ("cargo", re.compile(r"^test result: \w+\. (?P<body>.*)$")),
)
_UNITTEST_RAN = re.compile(r"^Ran (?P<total>\d+) tests? in ")
_UNITTEST_END = re.compile(r"^(?P<ok>OK|FAILED)(?: \((?P<body>[^)]*)\))?\s*$")

_FAILED_NAME_PATTERNS = (
    re.compile(r"^\s*(?:FAIL|×|✗|✕)\s+(?P<name>\S.*?)(?:\s+\d+\s*ms)?\s*$"),  # vitest/jest
    re.compile(r"^\s*●\s+(?P<name>.+?)\s*$"),                                   # jest
    re.compile(r"^FAILED (?P<name>\S+::\S+?)(?: - .*)?$"),                     # pytest
    re.compile(r"^(?:FAIL|ERROR): (?P<name>\S.*)$"),                             # unittest
    re.compile(r"^\s*--- FAIL: (?P<name>\S+)"),                                  # go
    re.compile(r"^test (?P<name>\S+) \.\.\. FAILED$"),                           # cargo
)
_FAILURE_START = re.compile(
    r"(AssertionError|Error:|Expected|Received|expect\(|assert\b|Traceback|panicked|"
    r"^\s*(?:FAIL|×|✗|●)\s|^FAILED |^(?:FAIL|ERROR): |--- FAIL:|error(?:\[\w+\])?:|ERR!)")


def strip_ansi(text):
    return ANSI.sub("", text)


def _numbers(body):
    found = {}
    for value, word in re.findall(r"(\d+)\s+(passed|failed|skipped|errors?|todo|ignored|xfailed|xpassed)", body):
        key = {"error": "errors", "ignored": "skipped", "todo": "skipped"}.get(word, word)
        found[key] = found.get(key, 0) + int(value)
    return found


def counts(lines):
    """Best-effort counts from the last recognizable summary line."""
    result = None
    for i, line in enumerate(lines):
        for runner, pattern in _COUNT_PATTERNS:
            m = pattern.match(line)
            if m:
                data = _numbers(m.group("body"))
                if data:
                    total = m.groupdict().get("total")
                    if total:
                        data["total"] = int(total)
                    data["runner"] = runner
                    result = data
        m = _UNITTEST_RAN.match(line)
        if m:
            data = {"runner": "unittest", "total": int(m.group("total"))}
            for later in lines[i + 1:i + 6]:
                end = _UNITTEST_END.match(later.strip())
                if end:
                    for key, value in re.findall(r"(\w+)=(\d+)", end.group("body") or ""):
                        data[key] = int(value)
                    data["status"] = end.group("ok")
                    break
            result = data
    return result


def failed_names(lines):
    names = []
    for line in lines:
        for pattern in _FAILED_NAME_PATTERNS:
            m = pattern.match(line)
            if m:
                name = m.group("name").strip()
                if not name or "\t" in name or re.fullmatch(r"[\d.]+\s*m?s", name):
                    break  # go prints "FAIL<TAB>package<TAB>time" per package, not a test name
                # vitest prints both "× suite > test" and "FAIL file > suite > test": keep the qualified one
                for i, known in enumerate(names):
                    if known == name or known.endswith(" > " + name):
                        break
                    if name.endswith(" > " + known):
                        names[i] = name
                        break
                else:
                    names.append(name)
                break
    return names


def first_failure(lines):
    for i, line in enumerate(lines):
        if _FAILURE_START.search(line):
            start = max(0, i - 2)
            return start + 1, lines[start:start + EXCERPT_LINES]
    return None, []


def digest(raw_text, *, exit_code, passthrough_bytes=PASSTHROUGH_BYTES):
    """Return the inline view of one command's combined output."""
    text = strip_ansi(raw_text)
    size = len(text.encode("utf-8"))
    lines = text.splitlines()
    view = {"exit_code": exit_code, "output_bytes": size, "output_lines": len(lines)}
    if size <= passthrough_bytes:
        view["output"] = text
        view["shaped"] = False
        return view
    view["shaped"] = True
    c = counts(lines)
    if c:
        view["counts"] = c
    names = failed_names(lines)
    if names:
        view["failed_tests"] = names[:MAX_FAILED_NAMES]
        if len(names) > MAX_FAILED_NAMES:
            view["failed_tests_omitted"] = len(names) - MAX_FAILED_NAMES
    if exit_code != 0:
        line_no, excerpt = first_failure(lines)
        if excerpt:
            view["first_failure"] = {"line": line_no, "text": "\n".join(excerpt)}
    tail = [line for line in lines if line.strip()][-TAIL_LINES:]
    view["tail"] = "\n".join(tail)
    view["complete"] = False
    return view
