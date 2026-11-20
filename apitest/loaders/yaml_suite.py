"""Loads ``*.api.yaml`` suites: API URLs described directly in YAML."""
from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from ..assertions import EXPECT_KEYS
from ..model import HTTP_METHODS, CaseSpec, SuiteSpec

SUITE_KEYS = {"name", "base_url", "variables", "defaults", "tests", "smoke", "tags"}
DEFAULT_KEYS = {"headers", "params", "auth", "timeout", "expect"}
CASE_KEYS = {"name", "method", "url", "path", "headers", "params", "json", "body", "form", "multipart",
             "auth", "timeout", "expect", "save", "tags", "skip", "repeat", "cases", "data_file",
             "variables", "notes", *HTTP_METHODS}


class SuiteError(ValueError):
    pass


def load(path: Path) -> SuiteSpec:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SuiteError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise SuiteError(f"{path}: top level must be a mapping with a 'tests' and/or 'smoke' list")
    return from_dict(doc, path)


def from_dict(doc: Dict[str, Any], path: Path) -> SuiteSpec:
    _check_keys(doc, SUITE_KEYS, f"{path}: suite")
    defaults = doc.get("defaults") or {}
    _check_keys(defaults, DEFAULT_KEYS, f"{path}: defaults")
    suite = SuiteSpec(
        name=str(doc.get("name") or path.name.split(".")[0]),
        source=path,
        kind="yaml",
        base_url=doc.get("base_url", "{{baseUrl}}"),
        variables=dict(doc.get("variables") or {}),
        tags=list(doc.get("tags") or []),
    )
    if "tests" not in doc and "smoke" not in doc:
        raise SuiteError(f"{path}: suite has neither 'tests' nor 'smoke'")

    for i, entry in enumerate(doc.get("smoke") or []):
        suite.cases.append(_smoke_case(entry, defaults, suite, i, path))

    for i, raw in enumerate(doc.get("tests") or []):
        if not isinstance(raw, dict):
            raise SuiteError(f"{path}: tests[{i}] must be a mapping")
        where = f"{path}: test '{raw.get('name', i + 1)}'"
        _check_keys(raw, CASE_KEYS, where)
        suite.cases.extend(_expand(raw, defaults, suite, where))

    _dedupe_names(suite.cases)
    return suite


# ---------------------------------------------------------------- helpers

def _check_keys(d: Any, allowed: set, where: str) -> None:
    if not isinstance(d, dict):
        raise SuiteError(f"{where} must be a mapping")
    unknown = set(d) - allowed
    if unknown:
        raise SuiteError(f"{where}: unknown key(s) {sorted(unknown)}. Allowed: {sorted(allowed)}")


def _smoke_case(entry: Any, defaults: Dict[str, Any], suite: SuiteSpec, i: int, path: Path) -> CaseSpec:
    if isinstance(entry, str):
        parts = entry.split(None, 1)
        method, url = (parts[0].upper(), parts[1]) if len(parts) == 2 and parts[0].lower() in HTTP_METHODS else ("GET", entry)
        entry = {"method": method, "url": url}
    if not isinstance(entry, dict) or "url" not in entry:
        raise SuiteError(f"{path}: smoke[{i}] must be a URL string or a mapping with 'url'")
    expect = copy.deepcopy(defaults.get("expect") or {})
    if "status" in entry:
        expect["status"] = entry["status"]
    if "max_time_ms" in entry:
        expect["max_time_ms"] = entry["max_time_ms"]
    method = str(entry.get("method", "GET")).upper()
    return CaseSpec(
        name=entry.get("name") or f"{method} {entry['url']}",
        method=method,
        url=str(entry["url"]),
        headers=dict(defaults.get("headers") or {}),
        params=dict(defaults.get("params") or {}),
        auth=defaults.get("auth"),
        timeout=defaults.get("timeout"),
        expect=expect,
        tags=list(suite.tags) + ["smoke"],
    )


def _expand(raw: Dict[str, Any], defaults: Dict[str, Any], suite: SuiteSpec, where: str) -> List[CaseSpec]:
    base = _build_case(raw, defaults, suite, where)
    rows: List[Dict[str, Any]] = []
    if raw.get("cases") is not None:
        if not isinstance(raw["cases"], list):
            raise SuiteError(f"{where}: 'cases' must be a list of variable mappings")
        rows.extend(raw["cases"])
    if raw.get("data_file"):
        rows.extend(_load_data_file(suite.base_dir / raw["data_file"], where))

    out: List[CaseSpec] = []
    if rows:
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise SuiteError(f"{where}: case #{i} must be a mapping of variables")
            c = copy.deepcopy(base)
            c.variables = {**base.variables, **row}
            label = row.get("id") or row.get("case") or i
            c.name = f"{base.name} [{label}]"
            out.append(c)
    else:
        out.append(base)

    repeat = int(raw.get("repeat", 1))
    if repeat > 1:
        out = [_renamed(c, f"{c.name} #{n}") for c in out for n in range(1, repeat + 1)]
    return out


def _renamed(c: CaseSpec, name: str) -> CaseSpec:
    c2 = copy.deepcopy(c)
    c2.name = name
    return c2


def _build_case(raw: Dict[str, Any], defaults: Dict[str, Any], suite: SuiteSpec, where: str) -> CaseSpec:
    shorthand = [m for m in HTTP_METHODS if m in raw]
    if len(shorthand) > 1:
        raise SuiteError(f"{where}: use only one of {shorthand}")
    if shorthand:
        method, url = shorthand[0].upper(), raw[shorthand[0]]
    else:
        method = str(raw.get("method", "GET")).upper()
        url = raw.get("url", raw.get("path"))
    if url is None:
        raise SuiteError(f"{where}: needs a URL - e.g. 'get: /users' or 'method: GET' + 'url: ...'")

    expect = copy.deepcopy(defaults.get("expect") or {})
    for k, v in (raw.get("expect") or {}).items():
        if k in ("headers", "json") and isinstance(v, dict) and isinstance(expect.get(k), dict):
            expect[k] = {**expect[k], **v}
        else:
            expect[k] = v
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        raise SuiteError(f"{where}: unknown expect key(s) {sorted(unknown)}. Allowed: {sorted(EXPECT_KEYS)}")

    body_keys = [k for k in ("json", "body", "form", "multipart") if k in raw]
    if len(body_keys) > 1:
        raise SuiteError(f"{where}: use only one of {body_keys}")

    auth = raw.get("auth", defaults.get("auth"))
    if auth in ("none", False):
        auth = None

    save = raw.get("save") or {}
    if not isinstance(save, dict):
        raise SuiteError(f"{where}: 'save' must map variable names to JSON paths, e.g. {{userId: $.id}}")

    name = raw.get("name") or f"{method} {url}"
    return CaseSpec(
        name=str(name),
        method=method,
        url=str(url),
        headers={**(defaults.get("headers") or {}), **(raw.get("headers") or {})},
        params={**(defaults.get("params") or {}), **(raw.get("params") or {})},
        json=raw.get("json"),
        has_json="json" in raw,
        body=raw.get("body"),
        form=raw.get("form"),
        multipart=raw.get("multipart"),
        auth=auth,
        timeout=raw.get("timeout", defaults.get("timeout")),
        expect=expect,
        save={str(k): str(v) for k, v in save.items()},
        variables=dict(raw.get("variables") or {}),
        tags=list(suite.tags) + list(raw.get("tags") or []),
        skip=(str(raw["skip"]) if raw.get("skip") not in (None, False) else None),
    )


def _load_data_file(p: Path, where: str) -> List[Dict[str, Any]]:
    if not p.is_file():
        raise SuiteError(f"{where}: data_file not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        return [dict(r) for r in csv.DictReader(text.splitlines())]
    data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, list):
        raise SuiteError(f"{where}: data_file must contain a list of objects")
    return data


def _dedupe_names(cases: List[CaseSpec]) -> None:
    seen: Dict[str, int] = {}
    for c in cases:
        n = seen.get(c.name, 0) + 1
        seen[c.name] = n
        if n > 1:
            c.name = f"{c.name} ({n})"


# ---------------------------------------------------------------- export

def dump(suite: SuiteSpec) -> str:
    """Serialises a suite (e.g. an imported Postman collection) as editable YAML."""
    tests = []
    for c in suite.cases:
        t: Dict[str, Any] = {"name": c.name, c.method.lower(): c.url}
        if c.headers:
            t["headers"] = c.headers
        if c.params:
            t["params"] = c.params
        if c.has_json:
            t["json"] = c.json
        elif c.body is not None:
            t["body"] = c.body
        elif c.form is not None:
            t["form"] = c.form
        elif c.multipart is not None:
            t["multipart"] = c.multipart
        if c.auth:
            t["auth"] = c.auth
        if c.expect:
            t["expect"] = _simplify_expect(c.expect)
        if c.save:
            t["save"] = c.save
        if c.skip:
            t["skip"] = c.skip
        if c.warnings:
            t["notes"] = c.warnings
        tests.append(t)
    doc: Dict[str, Any] = {"name": suite.name}
    if suite.variables:
        doc["variables"] = suite.variables
    doc["tests"] = tests
    header = (f"# Generated from {suite.source.name} by 'python -m apitest import-postman'.\n"
              "# Edit freely: add assertions under 'expect', data-driven 'cases', etc.\n")
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


def _simplify_expect(expect: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(expect)
    for section in ("json", "headers"):
        for k, v in list((out.get(section) or {}).items()):
            if isinstance(v, dict) and set(v) == {"eq"} and not isinstance(v["eq"], dict):
                out[section][k] = v["eq"]
    return out
