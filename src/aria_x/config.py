"""Configuration loading for ARIA-X.

Precedence: dataclass defaults < TOML/JSON file < environment variables <
explicit overrides. The file is TOML on Python 3.11+; JSON is accepted as a
fallback for minimal environments.

A minimal engagement config looks like::

    [scope]
    authorized = true
    targets = ["127.0.0.1", "*.example.test"]

    [llm]
    provider = "openai"
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-chat"
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .util import as_list, json_loads, normalize_base_url, parse_bool

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - only on older interpreters
    _toml = None


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LLMConfig:
    provider: str = "mock"              # mock | scenario | openai | anthropic | ollama
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 120.0
    max_retries: int = 2
    tool_mode: str = "auto"             # auto | native | prompt
    json_mode: bool = True
    role_models: dict[str, str] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    scenario: str = ""                  # scenario provider script name/path

    def resolved_base_url(self) -> str:
        url = normalize_base_url(self.base_url)
        if not url and self.provider == "openai":
            url = "https://api.openai.com/v1"
        if not url and self.provider == "ollama":
            url = "http://127.0.0.1:11434/v1"
        return url

    def model_for(self, role: str) -> str:
        return self.role_models.get(role) or self.role_models.get("default") or self.model


@dataclass(slots=True)
class BudgetConfig:
    max_seconds: int = 1800
    max_llm_calls: int = 160
    max_tokens: int = 1_200_000
    max_tool_calls: int = 400
    max_steps_per_task: int = 18
    max_intents_per_reason: int = 3
    max_concurrency: int = 2
    max_total_intents: int = 40
    max_specialist_depth: int = 2


@dataclass(slots=True)
class SandboxConfig:
    backend: str = "local"              # local | docker | mock
    image: str = "kalilinux/kali-rolling"
    workdir: str = ""                   # resolved per-run when empty
    timeout: float = 120.0
    network: str = "bridge"             # bridge | none | host
    docker_binary: str = "docker"
    extra_args: list[str] = field(default_factory=list)
    max_output: int = 20000
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ScopeConfig:
    authorized: bool = False
    targets: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    allow_private: bool = True
    allow_loopback: bool = True
    max_rps: float = 3.0
    ports: list[int] = field(default_factory=lambda: [21, 22, 25, 53, 80, 110, 143, 443, 445,
                                                      3306, 3389, 5432, 6379, 8000, 8080, 8443, 8888, 9000])
    max_ports: int = 200
    notes: str = ""


@dataclass(slots=True)
class SafetyConfig:
    approval: str = "auto"              # auto | ask | deny
    block_destructive: bool = True
    extra_blocked_patterns: list[str] = field(default_factory=list)
    redact_secrets: bool = True
    allow_public_targets: bool = True
    max_command_seconds: float = 120.0
    note: str = ""


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token: str = ""
    open_browser: bool = False
    cors_origin: str = ""


@dataclass(slots=True)
class EngineConfig:
    bootstrap: bool = True
    reason_on_change: bool = True
    max_idle_rounds: int = 2
    retry_failed_tasks: int = 1
    require_finding_for_completion: bool = False
    language: str = "zh"
    planner_role: str = "planner"
    worker_role: str = "worker"
    specialist_role: str = "specialist"
    skills_dir: str = "skills"
    prompts_dir: str = "prompts"


@dataclass(slots=True)
class AriaXConfig:
    name: str = "ARIA-X engagement"
    data_dir: str = "data"
    runs_dir: str = "runs"
    log_level: str = "INFO"
    llm: LLMConfig = field(default_factory=LLMConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None, *,
             overrides: Mapping[str, Any] | None = None) -> "AriaXConfig":
        raw: dict[str, Any] = {}
        candidate = _find_config(path)
        if candidate is not None:
            raw = _read_config(candidate)
        cfg = cls.from_dict(raw)
        cfg.apply_env(os.environ)
        if overrides:
            cfg.merge(overrides)
        cfg.validate()
        return cfg

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AriaXConfig":
        if not raw:
            return cls()
        cfg = cls()
        for key, value in raw.items():
            if key in {"llm", "budget", "sandbox", "scope", "safety", "server", "engine"}:
                section = getattr(cfg, key)
                if not isinstance(value, Mapping):
                    raise ConfigError(f"config section '{key}' must be a table/object")
                _apply_section(section, value)
            elif hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def merge(self, raw: Mapping[str, Any]) -> None:
        for key, value in raw.items():
            if value is None:
                continue
            if key in {"llm", "budget", "sandbox", "scope", "safety", "server", "engine"} and isinstance(value, Mapping):
                _apply_section(getattr(self, key), value)
            elif hasattr(self, key):
                setattr(self, key, value)

    def apply_env(self, env: Mapping[str, str]) -> None:
        if env.get("ARIA_X_PROVIDER"):
            self.llm.provider = env["ARIA_X_PROVIDER"]
        if env.get("ARIA_X_BASE_URL"):
            self.llm.base_url = env["ARIA_X_BASE_URL"]
        if env.get("ARIA_X_API_KEY"):
            self.llm.api_key = env["ARIA_X_API_KEY"]
        if env.get("ARIA_X_MODEL"):
            self.llm.model = env["ARIA_X_MODEL"]
        if env.get("ARIA_X_DATA_DIR"):
            self.data_dir = env["ARIA_X_DATA_DIR"]
        if env.get("ARIA_X_AUTH_TOKEN"):
            self.server.auth_token = env["ARIA_X_AUTH_TOKEN"]
        if env.get("ARIA_X_LOG_LEVEL"):
            self.log_level = env["ARIA_X_LOG_LEVEL"]
        if env.get("ARIA_X_SANDBOX"):
            self.sandbox.backend = env["ARIA_X_SANDBOX"]
        if env.get("ARIA_X_APPROVAL"):
            self.safety.approval = env["ARIA_X_APPROVAL"]

    def validate(self) -> None:
        if self.llm.provider not in {"mock", "scenario", "openai", "anthropic", "ollama"}:
            raise ConfigError(f"unsupported llm.provider '{self.llm.provider}'")
        if self.llm.provider in {"openai", "anthropic"} and not self.llm.resolved_base_url():
            raise ConfigError("llm.base_url is required for remote providers")
        if self.llm.provider == "openai" and not self.llm.api_key and "localhost" not in self.llm.resolved_base_url() \
                and "127.0.0.1" not in self.llm.resolved_base_url():
            raise ConfigError("llm.api_key is required (or set ARIA_X_API_KEY)")
        if self.sandbox.backend not in {"local", "docker", "mock"}:
            raise ConfigError(f"unsupported sandbox.backend '{self.sandbox.backend}'")
        if self.safety.approval not in {"auto", "ask", "deny"}:
            raise ConfigError("safety.approval must be auto|ask|deny")
        if self.budget.max_concurrency < 1:
            raise ConfigError("budget.max_concurrency must be >= 1")
        if self.scope.ports and self.scope.max_ports < len(self.scope.ports):
            self.scope.max_ports = len(self.scope.ports)
        self.data_dir = str(Path(self.data_dir).expanduser().resolve())
        self.runs_dir = str(Path(self.runs_dir).expanduser().resolve())

    # -- helpers ---------------------------------------------------------

    def require_authorization(self) -> None:
        if not self.scope.authorized:
            raise ConfigError(
                "refusing to start: scope.authorized is false. ARIA-X only operates on assets you are "
                "explicitly authorized to test (set scope.authorized = true after written approval)."
            )
        if not self.scope.targets:
            raise ConfigError("refusing to start: scope.targets is empty")

    def public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        key = data.get("llm", {}).get("api_key") or ""
        if key:
            data["llm"]["api_key"] = key[:4] + "…" + key[-2:] if len(key) > 8 else "***"
        token = data.get("server", {}).get("auth_token") or ""
        if token:
            data["server"]["auth_token"] = "***"
        return data

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        """Write the config back as JSON (TOML writer is intentionally omitted)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        import json
        p.write_text(json.dumps(self.public_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "aria-x.db"


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

_SECTION_TYPES = {
    "llm": LLMConfig, "budget": BudgetConfig, "sandbox": SandboxConfig,
    "scope": ScopeConfig, "safety": SafetyConfig, "server": ServerConfig,
    "engine": EngineConfig,
}


def _coerce(value: Any, template: Any) -> Any:
    if isinstance(template, bool):
        return parse_bool(value)
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return template
    if isinstance(template, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return template
    if isinstance(template, list):
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return list(value) if isinstance(value, (list, tuple)) else template
    if isinstance(template, dict):
        return dict(value) if isinstance(value, Mapping) else template
    return value


def _apply_section(section: Any, values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        if not hasattr(section, key):
            continue
        current = getattr(section, key)
        if key == "ports" or key == "targets" or key == "deny" or key == "extra_args":
            setattr(section, key, _coerce(value, current))
        else:
            setattr(section, key, _coerce(value, current))


def _find_config(path: str | Path | None) -> Path | None:
    if path:
        p = Path(path).expanduser()
        if p.is_file():
            return p
        raise ConfigError(f"config file not found: {p}")
    env_path = os.environ.get("ARIA_X_CONFIG")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for candidate in ("configs/aria-x.toml", "aria-x.toml", "configs/aria-x.json", "aria-x.json"):
        p = Path(candidate)
        if p.is_file():
            return p
    return None


def _read_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        data = json_loads(text, {})
        if not isinstance(data, dict):
            raise ConfigError(f"invalid JSON config: {path}")
        return data
    if _toml is None:
        raise ConfigError("TOML config requires Python 3.11+ (or use a .json config)")
    try:
        data = _toml.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError
        raise ConfigError(f"invalid TOML config: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"invalid TOML config: {path}")
    return data
