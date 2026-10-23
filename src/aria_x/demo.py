"""Offline, deterministic end-to-end demo.

The demo proves the whole stack without an API key: a real local target, real
HTTP tools, real scope enforcement, the real orchestrator, and a scripted model
that behaves like a careful pentester. The scripted policy is intentionally
transparent — it is a test double, not a hidden "fake success".
"""

from __future__ import annotations

import json
import os
import re
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .llm.base import ChatMessage, LLMResponse
from .util import json_dumps, truncate

FLAG = "ARIA-X{demo_path_traversal_confirmed}"
TRAVERSAL_PATH = "/files?path=../flag.txt"


class DemoPolicy:
    """Deterministic policy that drives planner + worker through one finding."""

    def __init__(self, base_url: str, *, language: str = "zh") -> None:
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.flag_found = False
        self.planner_calls = 0
        self.worker_runs = 0

    # -- provider interface ---------------------------------------------

    def __call__(self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]] | None) -> dict[str, Any]:
        system = _system_text(messages)
        convo = "\n".join(m.content or "" for m in messages)
        if "PLANNER of ARIA-X" in system:
            return self._planner(convo)
        if "BOOTSTRAP agent" in system:
            return self._bootstrap(messages, convo)
        if "REPORTING agent" in system:
            return {"content": self._report(convo)}
        if "specialist in ARIA-X" in system:
            return {"content": "No specialist escalation needed; the local HTTP probe is sufficient."}
        return self._worker(messages, convo)

    # -- roles -----------------------------------------------------------

    def _planner(self, convo: str) -> dict[str, Any]:
        self.planner_calls += 1
        if self.flag_found:
            payload = {
                "action": "complete",
                "reasoning": "The traversal proof is recorded; the engagement goal is met.",
                "complete": {
                    "summary": "Confirmed unauthenticated path traversal on the demo /files endpoint.",
                    "proof": ["finding"],
                },
                "todos": [
                    {"title": "Fingerprint demo web app", "status": "done"},
                    {"title": "Test /files for path traversal", "status": "done"},
                ],
            }
            return {"content": json_dumps(payload)}
        todo_ids = re.findall(r"\((t_[0-9a-f]+)\)", convo)
        payload = {
            "action": "intents",
            "reasoning": "No fact resolves the goal yet; probe the only discovered endpoint with a traversal payload.",
            "intents": [{
                "description": (
                    f"Against {self.base_url}: fetch / to fingerprint the app, then test /files?path= "
                    "with a ../ traversal toward flag.txt and record the response."
                ),
                "priority": 1,
                "todo_id": todo_ids[0] if todo_ids else "",
            }],
            "todos": [
                {"title": "Fingerprint demo web app", "status": "active"},
                {"title": "Test /files for path traversal", "depends_on": []},
            ],
        }
        return {"content": json_dumps(payload)}

    def _bootstrap(self, messages: Sequence[ChatMessage], convo: str) -> dict[str, Any]:
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            return _tool_call("http_request", {"url": self.base_url + "/", "method": "GET"})
        return {"content": json_dumps(self._fact_payload(convo, negative=True))}

    def _worker(self, messages: Sequence[ChatMessage], convo: str) -> dict[str, Any]:
        tool_msgs = [m for m in messages if m.role == "tool"]
        if len(tool_msgs) == 0:
            return _tool_call("http_request", {"url": self.base_url + "/", "method": "GET"})
        if len(tool_msgs) == 1:
            return _tool_call("http_request", {"url": self.base_url + TRAVERSAL_PATH, "method": "GET"})
        self.worker_runs += 1
        if FLAG in convo or "ARIA-X{" in convo:
            self.flag_found = True
            return {"content": json_dumps(self._fact_payload(convo, negative=False))}
        return {"content": json_dumps(self._fact_payload(convo, negative=True))}

    # -- payloads --------------------------------------------------------

    def _fact_payload(self, convo: str, *, negative: bool) -> dict[str, Any]:
        if negative:
            return {
                "fact": {
                    "title": "Demo app reachable; traversal not proven",
                    "body": (
                        f"Fetched {self.base_url}/ and {self.base_url}{TRAVERSAL_PATH} but did not observe "
                        "the flag marker. The probe may have been refused or the target changed."
                    ),
                    "confidence": "medium",
                },
                "findings": [],
                "assets": [
                    {"kind": "host", "value": "127.0.0.1"},
                    {"kind": "endpoint", "value": self.base_url + "/files"},
                ],
                "goal_proven": False,
                "summary": "Probe completed without proof.",
            }
        evidence = _excerpt_around(convo, "ARIA-X{", 240)
        return {
            "fact": {
                "title": "Path traversal in /files leaks flag.txt",
                "body": (
                    f"GET {self.base_url}{TRAVERSAL_PATH} returned the demo flag. "
                    f"Response excerpt: {evidence}"
                ),
                "confidence": "high",
            },
            "findings": [{
                "title": "Unauthenticated path traversal in /files (CWE-22)",
                "severity": "high",
                "body": (
                    "The /files handler joins an attacker-controlled path with the public directory "
                    "without normalization, allowing ../ traversal to read files outside the web root. "
                    f"Reproduction: GET {self.base_url}{TRAVERSAL_PATH} -> 200; response contained {FLAG}. "
                    "Impact: disclosure of arbitrary readable files; the demo flag proves the primitive. "
                    "Remediation: canonicalize and confine paths to the public root, reject traversal, "
                    "and serve static files through a hardened handler."
                ),
                "cvss": 7.5,
                "asset_values": [self.base_url + "/files", "127.0.0.1"],
                "evidence": f"GET {self.base_url}{TRAVERSAL_PATH} -> HTTP 200; body contained {FLAG}",
            }],
            "assets": [
                {"kind": "host", "value": "127.0.0.1"},
                {"kind": "endpoint", "value": self.base_url + "/files"},
                {"kind": "artifact", "value": "flag.txt"},
            ],
            "goal_proven": True,
            "summary": "Confirmed and recorded a high-severity path traversal.",
        }

    def _report(self, convo: str) -> str:
        return (
            "# ARIA-X Demo Engagement\n\n"
            "## Executive summary\n"
            "An unauthenticated path traversal was confirmed against the local demo service; "
            "the flag file was readable outside the web root.\n\n"
            "## Finding\n"
            "- **High — Unauthenticated path traversal (/files, CWE-22)**\n"
            f"- Evidence: `GET {self.base_url}{TRAVERSAL_PATH}` returned `{FLAG}`.\n"
            "- Remediation: canonicalize and confine paths to the public root.\n"
        )


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments}]}


def _system_text(messages: Sequence[ChatMessage]) -> str:
    return "\n".join(m.content or "" for m in messages if m.role == "system")


def _excerpt_around(text: str, needle: str, radius: int) -> str:
    index = text.find(needle)
    if index < 0:
        return truncate(text.replace("\n", " "), 240)
    start = max(0, index - radius // 2)
    end = min(len(text), index + radius // 2)
    return text[start:end].replace("\n", " ")


# ---------------------------------------------------------------------------
# target process management
# ---------------------------------------------------------------------------


class DemoTarget:
    """Launch and stop the bundled intentionally-vulnerable target."""

    def __init__(self, script: Path, *, python: str | None = None) -> None:
        self.script = script
        self.python = python or sys.executable
        self.proc: subprocess.Popen[str] | None = None
        self.url = ""
        self.port = 0

    def start(self, timeout: float = 15.0) -> "DemoTarget":
        if not self.script.is_file():
            raise FileNotFoundError(f"demo target not found: {self.script}")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [self.python, str(self.script), "--port", "0", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            cwd=str(self.script.parent),
        )
        lines: "queue.Queue[str]" = queue.Queue()

        def _reader() -> None:
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                lines.put(line)

        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise RuntimeError(f"demo target exited early: {err[:800]}")
                continue
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("port"):
                self.port = int(payload["port"])
                self.url = str(payload.get("url") or f"http://127.0.0.1:{self.port}")
                return self
        self.stop()
        raise TimeoutError("demo target did not report a port in time")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self.proc = None

    def __enter__(self) -> "DemoTarget":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
