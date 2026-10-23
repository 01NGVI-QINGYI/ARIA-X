"""ARIA-X command line interface.

The CLI is intentionally small: it wires configuration, persistence, the event
bus and the engine together, and leaves policy in the library. Commands are
designed to be scriptable (stable exit codes, `--json` where useful).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Iterable

from . import __version__, TAGLINE
from .config import AriaXConfig, BudgetConfig, EngineConfig, LLMConfig, SafetyConfig, ScopeConfig
from .errors import AriaXError, ConfigError
from .events import Event, EventBus
from .models import Project, Usage
from .store import Store
from .util import json_dumps, json_loads, now_iso, slugify
from dataclasses import asdict


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aria-x",
        description=f"ARIA-X {__version__} — {TAGLINE}. Authorized security research only.",
        epilog="Start with: aria-x demo    |    Console: aria-x serve    |    Docs: docs/",
    )
    parser.add_argument("-c", "--config", help="path to aria-x.toml/json (default: configs/aria-x.toml)")
    parser.add_argument("--version", action="version", version=f"ARIA-X {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # Allow -c/--config both before and after the subcommand (SUPPRESS keeps the
    # global value when the subcommand omits it).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="path to aria-x.toml/json")

    p = sub.add_parser("init", help="write a starter config", parents=[common])
    p.add_argument("--dir", default=".", help="directory to write configs/aria-x.toml into")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("demo", help="run the offline end-to-end demo (no API key needed)", parents=[common])
    p.add_argument("--serve", action="store_true", help="open the web console after the demo")
    p.add_argument("--port", type=int, default=8787, help="console port when --serve is used")
    p.add_argument("--keep", action="store_true", help="keep the demo target running (prints its URL)")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("run", help="run an engagement headlessly", parents=[common])
    p.add_argument("--goal", help="engagement goal (defaults to the project goal)")
    p.add_argument("--name", help="project name for a new project")
    p.add_argument("--project-id", help="reuse an existing project")
    p.add_argument("--provider", help="override llm.provider (mock|scenario|openai|anthropic|ollama)")
    p.add_argument("--model", help="override llm.model")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print the result as JSON")

    p = sub.add_parser("serve", help="start the web console", parents=[common])
    p.add_argument("--host", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="bind port (default 8787)")
    p.add_argument("--open", action="store_true", help="open a browser")

    p = sub.add_parser("doctor", help="check the local installation", parents=[common])
    p.add_argument("--llm", action="store_true", help="make a live LLM call to test credentials")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("tools", help="list the tool catalog", parents=[common])
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("projects", help="list projects", parents=[common])
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("graph", help="print a project's blackboard summary", parents=[common])
    p.add_argument("project_id", nargs="?")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="show the latest report for a run", parents=[common])
    p.add_argument("run_id", nargs="?")
    p.add_argument("--project-id")
    p.add_argument("--open", action="store_true")

    p = sub.add_parser("export", help="export a project as JSON", parents=[common])
    p.add_argument("project_id", nargs="?")
    p.add_argument("-o", "--output")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init, "demo": cmd_demo, "run": cmd_run, "serve": cmd_serve,
        "doctor": cmd_doctor, "tools": cmd_tools, "projects": cmd_projects,
        "graph": cmd_graph, "report": cmd_report, "export": cmd_export,
    }
    try:
        return int(handlers[args.command](args) or 0)
    except KeyboardInterrupt:
        print("\n[aria-x] interrupted", file=sys.stderr)
        return 130
    except AriaXError as exc:
        print(f"[aria-x] {exc.code}: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json_dumps(exc.details, indent=2), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[aria-x] file not found: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def load_config(args: argparse.Namespace, **overrides: Any) -> AriaXConfig:
    config = AriaXConfig.load(getattr(args, "config", None), overrides=overrides)
    if getattr(args, "provider", None):
        config.llm.provider = args.provider
    if getattr(args, "model", None):
        config.llm.model = args.model
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "port", None) and getattr(args, "command", "") == "serve":
        config.server.port = args.port
    config.validate()
    return config


def open_store(config: AriaXConfig) -> Store:
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    return Store(config.db_path)


def open_bus(config: AriaXConfig) -> EventBus:
    log_path = Path(config.data_dir) / "events.jsonl"
    return EventBus(log_path=log_path, redact=config.safety.redact_secrets)


def print_banner(config: AriaXConfig | None = None) -> None:
    print(f"ARIA-X {__version__} — {TAGLINE}")
    if config is not None:
        print(f"  data: {config.data_dir}")
        print(f"  provider: {config.llm.provider} / {config.llm.model}")


def latest_project(store: Store) -> Project | None:
    projects = store.list_projects(limit=1)
    return projects[0] if projects else None


def _provider(config: AriaXConfig, *, scenario: str | None = None) -> Any:
    """Build a provider instance when the CLI needs to inject one."""
    from .llm.router import build_provider
    if scenario:
        config.llm.provider = "scenario"
        config.llm.scenario = scenario
    return build_provider(config.llm)


class EventPrinter:
    """Consume the bus and print a human flight recorder."""

    ICONS = {
        "run.started": "▶", "run.finished": "■", "run.failed": "✖", "run.paused": "Ⅱ",
        "task.started": "→", "task.finished": "✓", "task.failed": "✗",
        "finding.created": "★", "asset.found": "◇", "node.created": "·",
        "todo.updated": "☑", "report.ready": "⤓", "budget": "…", "error": "!",
        "approval.requested": "?", "hint": "✎",
    }

    def __init__(self, bus: EventBus, *, verbose: bool = False, quiet: bool = False) -> None:
        self.bus = bus
        self.verbose = verbose
        self.quiet = quiet
        self.sub = bus.subscribe(maxsize=1000)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="aria-x-event-printer", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            event = self.sub.get(timeout=0.3)
            if event is not None:
                self.render(event)

    def render(self, event: Event) -> None:
        if self.quiet:
            return
        data = event.data or {}
        if event.type == "trace":
            return
        if event.type in {"llm.call", "tool.call"} and not self.verbose:
            return
        icon = self.ICONS.get(event.type, "·")
        line = self._describe(event.type, data)
        if line:
            print(f"  {icon} {line}")

    def _describe(self, type_: str, data: dict[str, Any]) -> str:
        if type_ == "run.started":
            return f"run {data.get('run_id', '')[:14]} started — {data.get('goal', '')[:100]}"
        if type_ == "run.finished":
            return f"run finished: {data.get('status')} — {data.get('summary', '')[:120]}"
        if type_ == "run.failed":
            return f"run failed: {data.get('error', '')[:160]}"
        if type_ == "task.started":
            return f"task {data.get('type')} / {data.get('agent', '')} started"
        if type_ == "task.finished":
            return f"task {data.get('type')} done in {data.get('duration_ms', 0)}ms"
        if type_ == "task.failed":
            return f"task {data.get('type')} failed: {data.get('error', '')[:140]}"
        if type_ == "finding.created":
            return f"[{str(data.get('severity', 'info')).upper()}] {data.get('title', '')[:120]}"
        if type_ == "asset.found":
            return f"{data.get('kind', '')}: {data.get('value', '')[:100]}"
        if type_ == "node.created":
            return f"{data.get('kind')}: {data.get('title', '')[:110]}"
        if type_ == "todo.updated":
            return f"todo {data.get('status')}: {data.get('title', '')[:100]}"
        if type_ == "report.ready":
            return f"report: {data.get('path') or data.get('markdown') or data.get('paths', {}).get('markdown', '')}"
        if type_ == "budget":
            usage = data.get("usage", {})
            return f"budget: {usage.get('llm_calls', 0)} calls / {usage.get('total_tokens', 0)} tokens / {data.get('elapsed_seconds', 0)}s"
        if type_ == "error":
            return f"error: {data.get('message', '')[:160]}"
        if type_ == "approval.requested":
            return f"approval needed: {data.get('tool')} ({data.get('risk')})"
        if type_ == "hint":
            return f"hint: {data.get('content', '')[:100]}"
        if self.verbose:
            return f"{type_}: {json_dumps(data, ensure_ascii=False)[:240]}"
        return ""

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=2)
        self.sub.close()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """# ARIA-X configuration — authorized testing only.
# Docs: docs/SECURITY.md and docs/DEPLOYMENT.md

name = "My engagement"
data_dir = "data"
runs_dir = "runs"
log_level = "INFO"

[scope]
# Refuse to start until you have written authorization for these targets.
authorized = false
targets = []                 # e.g. ["127.0.0.1", "*.staging.example.test", "10.0.0.0/24"]
deny = []
allow_private = true
allow_loopback = true
max_rps = 3.0

[llm]
provider = "openai"          # mock | scenario | openai | anthropic | ollama
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key = ""                 # or set ARIA_X_API_KEY
temperature = 0.2
max_tokens = 4096
tool_mode = "auto"

[budget]
max_seconds = 1800
max_llm_calls = 160
max_tokens = 1200000
max_tool_calls = 400
max_steps_per_task = 18
max_intents_per_reason = 3
max_concurrency = 2
max_total_intents = 40

[sandbox]
backend = "local"            # local | docker | mock
image = "kalilinux/kali-rolling"
network = "none"             # enable "bridge" only for an isolated lab
timeout = 120

[safety]
approval = "auto"            # auto | ask | deny
block_destructive = true
redact_secrets = true

[server]
host = "127.0.0.1"
port = 8787
auth_token = ""

[engine]
language = "zh"
bootstrap = true
max_idle_rounds = 2
"""


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir).expanduser() / "configs" / "aria-x.toml"
    if target.exists() and not args.force:
        print(f"[aria-x] config already exists: {target} (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"[aria-x] wrote {target}")
    print("[aria-x] next: edit scope.targets + llm settings, then `aria-x doctor`")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2]
    script = root / "examples" / "demo_target.py"
    if not script.is_file():
        print(f"[aria-x] demo target missing: {script}", file=sys.stderr)
        return 2

    from .demo import DemoPolicy, DemoTarget
    from .llm.mock import MockProvider

    stamp = time.strftime("%Y%m%d-%H%M%S")
    data_dir = root / "data" / f"demo-{stamp}"
    runs_dir = root / "runs" / f"demo-{stamp}"
    config = AriaXConfig(
        name="ARIA-X demo",
        data_dir=str(data_dir),
        runs_dir=str(runs_dir),
        llm=LLMConfig(provider="mock", model="demo-policy"),
        budget=BudgetConfig(max_seconds=120, max_llm_calls=40, max_tokens=200000,
                            max_tool_calls=60, max_steps_per_task=8, max_concurrency=1,
                            max_total_intents=4),
        scope=ScopeConfig(authorized=True, targets=["127.0.0.1"], allow_loopback=True, max_rps=10),
        safety=SafetyConfig(approval="auto", redact_secrets=True),
        engine=EngineConfig(language="zh", bootstrap=True, max_idle_rounds=1),
    )
    config.validate()

    print_banner(config)
    target = DemoTarget(script, python=sys.executable)
    store = open_store(config)
    bus = open_bus(config)
    printer = EventPrinter(bus, verbose=False, quiet=bool(getattr(args, 'quiet', False)))
    engagement = None
    try:
        target.start()
        print(f"  target: {target.url} (localhost-only intentionally vulnerable demo)")
        config.scope.targets = ["127.0.0.1", target.url]
        policy = DemoPolicy(target.url, language=config.engine.language)
        from .engine import Engagement
        engagement = Engagement(config, store=store, events=bus,
                                provider=MockProvider(policy=policy))
        project = engagement.create_project(
            "ARIA-X Demo — Path Traversal",
            "Prove whether the local demo web app exposes a file-disclosure vulnerability, "
            "capture reproducible evidence, and produce a report.",
            scope=asdict(config.scope),
        )
        print(f"  project: {project.id}")
        result = engagement.run(project.id)
        printer.stop()
        print()
        counts = store.counts(project.id)
        print(f"[aria-x] status={result.status}  findings={result.metrics.get('findings', counts['findings_by_severity'])}")
        if result.report_path:
            print(f"[aria-x] report: {result.report_path}")
        if result.error:
            print(f"[aria-x] error: {result.error}", file=sys.stderr)
        if args.keep:
            print(f"[aria-x] demo target left running at {target.url} (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        if args.serve:
            _serve_with(config, store, bus, engagement, port=args.port, open_browser=True)
        return 0 if result.ok else 1
    finally:
        try:
            printer.stop()
        except Exception:
            pass
        if engagement is not None:
            try:
                engagement.close()
            except Exception:
                pass
        target.stop()
        store.close()


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args)
    config.require_authorization()
    store = open_store(config)
    bus = open_bus(config)
    printer = EventPrinter(bus, verbose=False, quiet=args.quiet)
    engagement = None
    try:
        from .engine import Engagement
        engagement = Engagement(config, store=store, events=bus,
                                provider=_provider(config) if args.provider else None)
        if args.project_id:
            project = store.get_project(args.project_id)
            goal = args.goal or project.goal
        else:
            goal = args.goal or ""
            if not goal:
                print("[aria-x] --goal is required for a new project", file=sys.stderr)
                return 2
            project = engagement.create_project(
                args.name or f"Engagement {time.strftime('%Y-%m-%d %H:%M')}", goal,
                scope=asdict(config.scope),
            )
        print_banner(config)
        print(f"  project: {project.id}  goal: {goal[:100]}")
        result = engagement.run(project.id, goal=goal)
        printer.stop()
        if args.json:
            print(json_dumps(result.to_dict() if hasattr(result, "to_dict") else result.__dict__,
                             indent=2, ensure_ascii=False))
        else:
            print()
            print(f"[aria-x] status={result.status} run={result.run_id}")
            if result.summary:
                print(f"[aria-x] summary: {result.summary}")
            if result.report_path:
                print(f"[aria-x] report: {result.report_path}")
            if result.error:
                print(f"[aria-x] error: {result.error}", file=sys.stderr)
        return 0 if result.ok else 1
    finally:
        try:
            printer.stop()
        except Exception:
            pass
        if engagement is not None:
            try:
                engagement.close()
            except Exception:
                pass
        store.close()


def cmd_serve(args: argparse.Namespace) -> int:
    config = load_config(args)
    print_banner(config)
    store = open_store(config)
    bus = open_bus(config)
    from .engine import Engagement
    from .server import serve
    engagement = Engagement(config, store=store, events=bus)
    host, port = config.server.host, config.server.port
    print(f"  console: http://{host}:{port}")
    print("  (Ctrl-C to stop)")
    try:
        serve(config, engagement=engagement, store=store, bus=bus, block=True)
    finally:
        engagement.close()
        store.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    ok_all = True

    def check(name: str, ok: bool, detail: str, *, hard: bool = True) -> None:
        nonlocal ok_all
        checks.append({"check": name, "ok": ok, "detail": detail})
        if hard and not ok:
            ok_all = False

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        config = load_config(args)
        check("config", True, f"provider={config.llm.provider} model={config.llm.model}")
    except AriaXError as exc:
        check("config", False, str(exc))
        config = AriaXConfig()
        config.validate()
    try:
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        probe = Path(config.data_dir) / ".doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("data-dir", True, config.data_dir)
    except OSError as exc:
        check("data-dir", False, str(exc))
    check("authorization", config.scope.authorized and bool(config.scope.targets),
          "scope.authorized=true, targets=" + ", ".join(config.scope.targets[:5])
          if config.scope.authorized else "NOT configured (set scope.authorized + targets)")
    try:
        from .tools.builtin import build_registry  # noqa: F401
        check("tools", True, "tool registry importable")
    except Exception as exc:
        check("tools", False, f"tool layer unavailable: {exc}")
    try:
        from .engine import Engagement  # noqa: F401
        check("engine", True, "orchestrator importable")
    except Exception as exc:
        check("engine", False, f"engine unavailable: {exc}")
    try:
        from .server import serve  # noqa: F401
        check("server", True, "http server importable")
    except Exception as exc:
        check("server", False, f"server unavailable: {exc}")
    if config.sandbox.backend == "docker":
        from .tools.sandbox import build_sandbox
        sandbox = build_sandbox(config.sandbox, Path(tempfile.mkdtemp(prefix="aria-x-doctor-")))
        ready, detail = sandbox.ensure_ready()
        check("sandbox", ready, detail)
    else:
        check("sandbox", True, config.sandbox.backend)

    if args.llm:
        try:
            from .llm.base import ChatMessage
            from .llm.router import ModelRouter
            router = ModelRouter(config)
            response = router.chat("doctor", [ChatMessage.user("Reply with the single word OK.")],
                                   max_tokens=8, task_id="doctor")
            check("llm-live", True, (response.content or "").strip()[:80] or "(empty)")
        except Exception as exc:
            check("llm-live", False, str(exc)[:200])

    if args.json:
        print(json_dumps(checks, indent=2, ensure_ascii=False))
    else:
        print(f"ARIA-X {__version__} doctor")
        for item in checks:
            mark = "✓" if item["ok"] else "✗"
            print(f"  {mark} {item['check']:<14} {item['detail']}")
        print("\nAll core checks passed." if ok_all else "\nSome checks failed; fix the ✗ items above.")
    return 0 if ok_all else 1


def cmd_tools(args: argparse.Namespace) -> int:
    config = load_config(args)
    from .tools.builtin import build_registry
    from .tools.sandbox import build_sandbox
    from .tools.scope import Scope
    from .tools.base import ToolContext
    project = Project.new("catalog", "catalog")
    store = Store(":memory:")
    store.create_project(project)
    workdir = Path(tempfile.mkdtemp(prefix="aria-x-tools-"))
    ctx = ToolContext(project=project, run_id="r", task_id="t", agent="cli", role="cli",
                      store=store, config=config, events=EventBus(), scope=Scope(config.scope),
                      sandbox=build_sandbox(config.sandbox, workdir), workdir=workdir)
    try:
        registry = build_registry(ctx)
        catalog = registry.catalog()
        if args.json:
            print(json_dumps(catalog, indent=2, ensure_ascii=False))
        else:
            print(f"{len(catalog)} tools\n")
            for item in catalog:
                print(f"  {item['name']:<20} {item['risk']:<8} {'scope' if item['requires_scope'] else '':<6} {item['description'].splitlines()[0][:90]}")
        return 0
    finally:
        store.close()


def cmd_projects(args: argparse.Namespace) -> int:
    config = load_config(args)
    store = open_store(config)
    try:
        projects = store.list_projects()
        if args.json:
            print(json_dumps([p.to_dict() for p in projects], indent=2, ensure_ascii=False))
        else:
            if not projects:
                print("No projects yet. Run `aria-x demo` or `aria-x run --goal ...`.")
            for p in projects:
                counts = store.counts(p.id)
                print(f"  {p.id}  {p.status:<8}  {p.name[:46]:<46}  "
                      f"facts={counts['nodes'].get('fact', 0)} findings={sum(counts['findings_by_severity'].values())}")
        return 0
    finally:
        store.close()


def cmd_graph(args: argparse.Namespace) -> int:
    config = load_config(args)
    store = open_store(config)
    try:
        project = store.get_project(args.project_id) if args.project_id else latest_project(store)
        if project is None:
            print("[aria-x] no projects", file=sys.stderr)
            return 2
        data = store.graph_overview(project.id)
        if args.json:
            print(json_dumps(data, indent=2, ensure_ascii=False))
            return 0
        counts = data["counts"]
        print(f"Project {project.id} — {project.name}")
        print(f"Goal: {project.goal}")
        print(f"Nodes: {counts['nodes']}  edges={counts['edges']}  traces={counts['traces']}")
        print(f"Findings: {counts['findings_by_severity']}")
        print("\nRecent facts:")
        for node in [n for n in data["nodes"] if n["kind"] == "fact"][-8:]:
            print(f"  · {node['title'][:100]}")
        if data["todos"]:
            print("\nPlan:")
            for todo in data["todos"]:
                print(f"  [{todo['status']:<7}] {todo['title'][:90]}")
        return 0
    finally:
        store.close()


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args)
    runs_dir = Path(config.runs_dir)
    if args.run_id:
        candidates = [runs_dir / args.run_id]
    elif args.project_id:
        candidates = sorted(runs_dir.glob(f"{args.project_id}*"), reverse=True)
    else:
        candidates = sorted([p for p in runs_dir.iterdir() if p.is_dir()], reverse=True) if runs_dir.is_dir() else []
    for candidate in candidates:
        html = candidate / "report.html"
        md = candidate / "report.md"
        if html.is_file() or md.is_file():
            target = html if html.is_file() else md
            print(str(target))
            if args.open:
                webbrowser.open(target.resolve().as_uri())
            return 0
    print("[aria-x] no report found", file=sys.stderr)
    return 2


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args)
    store = open_store(config)
    try:
        project = store.get_project(args.project_id) if args.project_id else latest_project(store)
        if project is None:
            print("[aria-x] no projects", file=sys.stderr)
            return 2
        data = store.export_project(project.id)
        out = Path(args.output) if args.output else Path(config.data_dir) / f"{project.id}-export.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json_dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(str(out))
        return 0
    finally:
        store.close()


def _serve_with(config: AriaXConfig, store: Store, bus: EventBus, engagement: Any, *,
                port: int, open_browser: bool) -> None:
    from .server import serve
    config.server.port = port
    url = f"http://{config.server.host}:{port}"
    print(f"[aria-x] console: {url}")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
    serve(config, engagement=engagement, store=store, bus=bus, block=True)
