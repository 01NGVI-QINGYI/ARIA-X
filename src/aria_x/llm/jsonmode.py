"""Robust JSON extraction for model output.

Even with ``response_format`` and native tool calling, small/local models wrap
JSON in prose or fences. These helpers extract the first balanced object and
apply narrowly-scoped repairs (trailing commas, smart quotes).
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def extract_json(text: str) -> Any:
    """Return the first JSON value found in *text*.

    Raises ``ValueError`` when no parseable value exists.
    """
    if text is None:
        raise ValueError("empty model output")
    raw = str(text).strip()
    if not raw:
        raise ValueError("empty model output")

    candidates: list[str] = []
    fence = _FENCE.search(raw)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    start_a = raw.find("[")
    end_a = raw.rfind("]")
    if start_a != -1 and end_a > start_a:
        candidates.append(raw[start_a:end_a + 1])

    errors: list[str] = []
    for candidate in candidates:
        for variant in _variants(candidate):
            try:
                return json.loads(variant)
            except ValueError as exc:
                errors.append(str(exc))
    raise ValueError(f"no valid JSON in model output ({errors[-1] if errors else 'empty'})")


def _variants(text: str) -> list[str]:
    out = [text.translate(_SMART_QUOTES).strip()]
    repaired = _TRAILING_COMMA.sub(r"\1", out[0])
    if repaired != out[0]:
        out.append(repaired)
    # Strip a leading label such as "Result: {...}".
    if ":" in out[0][:40]:
        tail = out[0].split(":", 1)[1].strip()
        if tail.startswith("{") or tail.startswith("["):
            out.append(tail)
    return out


def try_extract_json(text: str, default: Any = None) -> Any:
    try:
        return extract_json(text)
    except ValueError:
        return default


def repair_json(text: str) -> str:
    """Best-effort normalization used for logging/repair round-trips."""
    try:
        return json.dumps(extract_json(text), ensure_ascii=False)
    except ValueError:
        return text


def validate_shape(value: Any, shape: dict[str, Any]) -> list[str]:
    """Validate a small subset of JSON Schema and return human-readable errors."""
    errors: list[str] = []
    _validate(value, shape, "$", errors)
    return errors


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if types and not any(checks.get(t, lambda v: True)(value) for t in types):
        errors.append(f"{path}: expected {types}, got {type(value).__name__}")
        return
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required field missing")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                _validate(value[key], subschema, f"{path}.{key}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]", errors)
