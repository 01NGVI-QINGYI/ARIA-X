"""Small dependency-free helpers shared across ARIA-X."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{6,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|session)\s*[=:]\s*[\"']?)([^\s\"'&,}]{4,})"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b"), "[REDACTED_KEY]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b"), "[REDACTED_GH_TOKEN]"),
)


def now_iso() -> str:
    """UTC timestamp in ISO-8601 with a ``Z`` suffix."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def parse_iso(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def new_id(prefix: str, length: int = 12) -> str:
    """Short, sortable-enough identifier such as ``n_4f9c1a2b7d31``."""
    return f"{prefix}_{os.urandom(length // 2 + 1).hex()[:length]}"


def slugify(value: str, fallback: str = "item") -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:64] or fallback


def truncate(text: str, limit: int = 8000, marker: str = "\n…[truncated {n} chars]") -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + marker.format(n=omitted)


def redact(text: str) -> str:
    """Best-effort secret redaction for logs, traces and reports."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(value: Any) -> Any:
    """Recursively redact strings in a JSON-compatible structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def json_dumps(value: Any, *, indent: int | None = None, ensure_ascii: bool = False) -> str:
    return json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, default=_json_default, sort_keys=False)


def json_loads(text: str | bytes | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_digest(value: Any) -> str:
    return sha256_text(json_dumps(value, ensure_ascii=False))


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write(path: str | Path, data: str | bytes, *, mode: str | None = None) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    if mode:
        try:
            os.chmod(p, int(mode, 8))
        except OSError:
            pass
    return p


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return truncate(line, limit)


def human_bytes(n: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{n}B"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def approx_tokens(text: str) -> int:
    """Cheap token estimate used when a provider reports no usage."""
    if not text:
        return 0
    # ~4 chars/token for English, ~1.6 chars/token for CJK.
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int((len(text) - cjk) / 4 + cjk / 1.6) + 1


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")
