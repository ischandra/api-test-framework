"""Response expectations.

``expect`` keys:
  status        201 | [200, 204] | "2xx" | {gte: 200, lt: 300}      (default "2xx")
  max_time_ms   2000
  headers       {Content-Type: {contains: json}, X-Id: {exists: true}}
  json          {"$.id": {type: integer}, "$.email": "a@example.com", "$.items.length": {gt: 0}}
  body_contains "text" | ["a", "b"]
  schema        path/to/schema.json (relative to the suite file) | inline JSON Schema

A matcher is either a literal (equality) or a dict of operators:
  eq ne gt gte lt lte contains not_contains regex len type exists one_of not_empty
"""
from __future__ import annotations

import json as _json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import jsonpath

OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "regex",
             "len", "type", "exists", "one_of", "not_empty"}
EXPECT_KEYS = {"status", "max_time_ms", "headers", "json", "body_contains", "schema"}

_TYPES = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}

_MISSING = object()


def is_matcher(expected: Any) -> bool:
    return isinstance(expected, dict) and bool(expected) and set(expected) <= OPERATORS


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_eq(a[k], b[k]) for k in a)
    return a == b


def _short(v: Any, limit: int = 120) -> str:
    s = _json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else repr(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def match(actual: Any, expected: Any) -> Optional[str]:
    """Returns None when ``actual`` satisfies ``expected``, otherwise a reason.
    ``actual`` may be the module-level ``_MISSING`` sentinel."""
    if not is_matcher(expected):
        if actual is _MISSING:
            return "is missing"
        return None if _eq(actual, expected) else f"expected {_short(expected)}, got {_short(actual)}"

    if "exists" in expected:
        present = actual is not _MISSING
        if present != bool(expected["exists"]):
            return "is missing" if not present else f"should not exist, got {_short(actual)}"
        if not present:
            return None
    if actual is _MISSING:
        return "is missing"

    for op, exp in expected.items():
        err = _apply(op, actual, exp)
        if err:
            return err
    return None


def _apply(op: str, actual: Any, exp: Any) -> Optional[str]:
    try:
        if op == "exists":
            return None
        if op == "eq":
            return None if _eq(actual, exp) else f"expected {_short(exp)}, got {_short(actual)}"
        if op == "ne":
            return None if not _eq(actual, exp) else f"expected anything but {_short(exp)}"
        if op in ("gt", "gte", "lt", "lte"):
            ok = {"gt": actual > exp, "gte": actual >= exp, "lt": actual < exp, "lte": actual <= exp}[op]
            sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            return None if ok else f"expected {sym} {_short(exp)}, got {_short(actual)}"
        if op in ("contains", "not_contains"):
            if isinstance(actual, str):
                found = str(exp) in actual
            elif isinstance(actual, list):
                found = any(_eq(x, exp) for x in actual)
            elif isinstance(actual, dict):
                found = exp in actual
            else:
                return f"cannot check '{op}' on {_short(actual)}"
            if op == "contains":
                return None if found else f"expected to contain {_short(exp)}, got {_short(actual)}"
            return None if not found else f"expected not to contain {_short(exp)}, got {_short(actual)}"
        if op == "regex":
            return None if re.search(str(exp), str(actual)) else f"expected to match /{exp}/, got {_short(actual)}"
        if op == "len":
            if not hasattr(actual, "__len__"):
                return f"has no length: {_short(actual)}"
            err = match(len(actual), exp)
            return None if err is None else f"length {err}"
        if op == "type":
            types = exp if isinstance(exp, list) else [exp]
            unknown = [t for t in types if t not in _TYPES]
            if unknown:
                return f"unknown type(s) {unknown}; use one of {sorted(_TYPES)}"
            return None if any(_TYPES[t](actual) for t in types) else f"expected type {exp}, got {type(actual).__name__} {_short(actual)}"
        if op == "one_of":
            return None if any(_eq(actual, x) for x in exp) else f"expected one of {_short(exp)}, got {_short(actual)}"
        if op == "not_empty":
            empty = actual is None or (hasattr(actual, "__len__") and len(actual) == 0)
            return None if empty != bool(exp) else ("is empty" if exp else f"expected empty, got {_short(actual)}")
    except TypeError as e:
        return f"cannot compare {_short(actual)} with {_short(exp)} ({e})"
    return f"unknown operator '{op}'"


def _status_ok(status: int, expected: Any) -> Optional[str]:
    if isinstance(expected, str) and re.fullmatch(r"[1-5]xx", expected.strip().lower()):
        return None if str(status)[0] == expected.strip()[0] else f"expected {expected}, got {status}"
    if isinstance(expected, list):
        return None if status in expected else f"expected one of {expected}, got {status}"
    if is_matcher(expected):
        return match(status, expected)
    try:
        return None if status == int(expected) else f"expected {int(expected)}, got {status}"
    except (TypeError, ValueError):
        return f"invalid status expectation {_short(expected)}"


def response_json(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        return _MISSING


def check_response(resp, expect: Dict[str, Any], base_dir: Optional[Path] = None) -> List[str]:
    """Checks every expectation and returns all failures (empty list = pass)."""
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        return [f"unknown expect key(s) {sorted(unknown)}; allowed: {sorted(EXPECT_KEYS)}"]
    failures: List[str] = []

    err = _status_ok(resp.status_code, expect.get("status", "2xx"))
    if err:
        failures.append(f"status: {err}")

    if expect.get("max_time_ms") is not None:
        ms = resp.elapsed.total_seconds() * 1000
        if ms > float(expect["max_time_ms"]):
            failures.append(f"response time: {ms:.0f} ms exceeds {expect['max_time_ms']} ms")

    for name, exp in (expect.get("headers") or {}).items():
        actual = resp.headers.get(name, _MISSING)
        err = match(actual, exp)
        if err:
            failures.append(f"header {name}: {err}")

    body_contains = expect.get("body_contains")
    if body_contains is not None:
        for needle in body_contains if isinstance(body_contains, list) else [body_contains]:
            if str(needle) not in resp.text:
                failures.append(f"body: does not contain {_short(str(needle))}")

    json_exp = expect.get("json") or {}
    schema = expect.get("schema")
    if json_exp or schema is not None:
        doc = response_json(resp)
        if doc is _MISSING:
            failures.append("body: response is not valid JSON")
        else:
            for path, exp in json_exp.items():
                try:
                    actual = jsonpath.extract(doc, path)
                except jsonpath.PathNotFound:
                    actual = _MISSING
                except jsonpath.InvalidPath as e:
                    failures.append(str(e))
                    continue
                err = match(actual, exp)
                if err:
                    failures.append(f"{path}: {err}")
            if schema is not None:
                failures.extend(_check_schema(doc, schema, base_dir))
    return failures


def _check_schema(doc: Any, schema: Any, base_dir: Optional[Path]) -> List[str]:
    import jsonschema  # local import keeps startup fast when unused

    if isinstance(schema, str):
        p = Path(schema)
        if not p.is_absolute() and base_dir is not None:
            p = base_dir / p
        try:
            schema = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return [f"schema: cannot load {p}: {e}"]
    validator_cls = jsonschema.validators.validator_for(schema)
    errors = sorted(validator_cls(schema).iter_errors(doc), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors[:5]:
        where = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path)
        out.append(f"schema: {where}: {e.message}")
    if len(errors) > 5:
        out.append(f"schema: ... and {len(errors) - 5} more violation(s)")
    return out


class ApiCheckFailed(AssertionError):
    pass


def check(resp, expect: Optional[Dict[str, Any]] = None, *, base_dir: Optional[Path] = None, **kwargs: Any):
    """Assertion helper for code-based tests::

        check(resp, status=201, json={"$.id": {"type": "integer"}})
    """
    from .client import describe_exchange

    spec = dict(expect or {})
    spec.update(kwargs)
    failures = check_response(resp, spec, base_dir)
    if failures:
        raise ApiCheckFailed("\n".join(["", *("  x " + f for f in failures), "", describe_exchange(resp)]))
    return resp
