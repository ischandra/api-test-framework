"""Imports Postman collections (v2.0 / v2.1) and Postman environment files.

Requests, folders, variables, auth (with inheritance), bodies and path variables are
converted directly. Test scripts can't be executed (they're JavaScript), so common
``pm.*`` assertion patterns are translated into declarative expectations. Script lines
that can't be translated are reported as warnings, so nothing is silently ignored.
"""
from __future__ import annotations

import http
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model import CaseSpec, SuiteSpec

COLLECTION_MARKER = "schema.getpostman.com/json/collection"


class PostmanError(ValueError):
    pass


def is_collection(path: Path) -> bool:
    if path.name.endswith(".postman_collection.json"):
        return True
    if path.suffix.lower() != ".json" or path.stat().st_size > 50_000_000:
        return False
    try:
        with path.open(encoding="utf-8") as fh:
            return COLLECTION_MARKER in fh.read(4096)
    except (OSError, UnicodeDecodeError):
        return False


def load_environment(path: Path) -> Dict[str, Any]:
    """Reads a Postman environment / globals export into a flat dict."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    values = doc.get("values", doc.get("variable", []))
    return {v["key"]: v.get("value", "") for v in values if v.get("enabled", True) and not v.get("disabled")}


def load(path: Path) -> SuiteSpec:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PostmanError(f"{path}: invalid JSON: {e}") from e
    if "item" not in doc:
        raise PostmanError(f"{path}: not a Postman collection (no 'item' list)")
    info = doc.get("info") or {}
    suite = SuiteSpec(
        name=info.get("name") or path.stem,
        source=path,
        kind="postman",
        base_url=None,
        variables={v["key"]: v.get("value", "") for v in doc.get("variable", []) if not v.get("disabled")},
    )
    _walk(doc["item"], [], doc.get("auth"), _scripts(doc, "test"), _scripts(doc, "prerequest"), suite)
    seen: Dict[str, int] = {}
    for c in suite.cases:
        seen[c.name] = seen.get(c.name, 0) + 1
        if seen[c.name] > 1:
            c.name = f"{c.name} ({seen[c.name]})"
    return suite


def _scripts(node: Dict[str, Any], listen: str) -> List[str]:
    lines: List[str] = []
    for ev in node.get("event") or []:
        if ev.get("listen") == listen and not ev.get("disabled"):
            exec_ = (ev.get("script") or {}).get("exec") or []
            lines.extend(exec_.splitlines() if isinstance(exec_, str) else exec_)
    return lines


def _walk(items: List[Dict[str, Any]], prefix: List[str], auth: Any, tests: List[str], pre: List[str],
          suite: SuiteSpec) -> None:
    for it in items:
        name = it.get("name") or "request"
        if "item" in it:  # folder
            _walk(it["item"], prefix + [name], it.get("auth", auth), tests + _scripts(it, "test"),
                  pre + _scripts(it, "prerequest"), suite)
        else:
            suite.cases.append(_request(it, " / ".join(prefix + [name]), auth, tests + _scripts(it, "test"),
                                        pre + _scripts(it, "prerequest")))


def _request(it: Dict[str, Any], name: str, inherited_auth: Any, test_lines: List[str], pre_lines: List[str]) -> CaseSpec:
    req = it.get("request") or {}
    if isinstance(req, str):
        req = {"method": "GET", "url": req}
    case = CaseSpec(name=name, method=str(req.get("method", "GET")).upper(), url=_url(req.get("url")))

    for h in req.get("header") or []:
        if not h.get("disabled") and h.get("key"):
            case.headers[h["key"]] = h.get("value", "")

    _body(req.get("body"), case)

    auth = req.get("auth", inherited_auth)
    case.auth = _auth(auth, case)

    if any(l.strip() and not l.strip().startswith("//") for l in pre_lines):
        case.warnings.append("Pre-request script not executed: " + " | ".join(l.strip() for l in pre_lines if l.strip())[:300])

    translator = ScriptTranslator()
    translator.translate(test_lines)
    case.expect, case.save = translator.expect, translator.save
    case.warnings.extend(f"Untranslated test script line: {l}" for l in translator.untranslated)
    return case


def _url(url: Any) -> str:
    if url is None:
        return ""
    if isinstance(url, str):
        return url
    raw = url.get("raw")
    if not raw:
        host = url.get("host") or []
        host = ".".join(host) if isinstance(host, list) else str(host)
        path = url.get("path") or []
        path = "/".join(path) if isinstance(path, list) else str(path)
        proto = url.get("protocol")
        raw = (f"{proto}://" if proto else "") + host + ("/" + path if path else "")
        q = [f"{p['key']}={p.get('value', '')}" for p in url.get("query") or [] if not p.get("disabled")]
        if q:
            raw += "?" + "&".join(q)
    raw = raw.split("#", 1)[0]
    for var in url.get("variable") or []:
        if var.get("key"):
            raw = re.sub(r"(?<=/):" + re.escape(var["key"]) + r"(?=/|\?|$)", str(var.get("value", "")), raw)
    return raw


def _body(body: Optional[Dict[str, Any]], case: CaseSpec) -> None:
    if not body or body.get("disabled"):
        return
    mode = body.get("mode")
    if mode == "raw":
        raw = body.get("raw") or ""
        if not raw.strip():
            return
        lang = (((body.get("options") or {}).get("raw") or {}).get("language") or "").lower()
        try:
            case.json, case.has_json = json.loads(raw), True
        except ValueError:
            case.body = raw
            if lang == "json" and not _has_header(case, "Content-Type"):
                case.headers["Content-Type"] = "application/json"
    elif mode == "urlencoded":
        case.form = {p["key"]: p.get("value", "") for p in body.get("urlencoded") or [] if not p.get("disabled")}
    elif mode == "formdata":
        case.multipart = {}
        for p in body.get("formdata") or []:
            if p.get("disabled"):
                continue
            if p.get("type") == "file":
                case.warnings.append(f"File upload field '{p.get('key')}' not supported - skipped")
            else:
                case.multipart[p["key"]] = p.get("value", "")
    elif mode == "graphql":
        gql = body.get("graphql") or {}
        payload: Dict[str, Any] = {"query": gql.get("query", "")}
        if gql.get("variables"):
            try:
                payload["variables"] = json.loads(gql["variables"])
            except ValueError:
                case.warnings.append("GraphQL variables are not valid JSON - skipped")
        case.json, case.has_json = payload, True
    elif mode:
        case.warnings.append(f"Body mode '{mode}' not supported - body not sent")


def _has_header(case: CaseSpec, name: str) -> bool:
    return any(k.lower() == name.lower() for k in case.headers)


def _kv(entries: Any) -> Dict[str, Any]:
    if isinstance(entries, dict):
        return entries
    return {e.get("key"): e.get("value") for e in entries or []}


def _auth(auth: Any, case: CaseSpec) -> Optional[Dict[str, Any]]:
    if not auth:
        return None
    kind = auth.get("type")
    if kind in (None, "noauth"):
        return None
    params = _kv(auth.get(kind))
    if kind == "bearer":
        return {"bearer": params.get("token", "")}
    if kind == "basic":
        return {"basic": {"username": params.get("username", ""), "password": params.get("password", "")}}
    if kind == "apikey":
        return {"api_key": {"name": params.get("key", "X-API-Key"), "value": params.get("value", ""),
                            "in": params.get("in", "header")}}
    if kind == "oauth2" and params.get("accessToken"):
        return {"bearer": params["accessToken"]}
    case.warnings.append(f"Auth type '{kind}' not supported - set the Authorization header yourself")
    return None


# ======================================================== script translation

_BOILERPLATE = re.compile(
    r"""^\s*(
        $ | // | /\* | \*
      | pm\.test\(\s*["'`][^"'`]*["'`]\s*,\s*(function\s*\(\s*\)|\(\s*\)\s*=>)\s*\{?\s*$
      | \}\s*\)?\s*;?\s*$
      | console\.\w+\(
      | (var|let|const)\s+\w+\s*=\s*(pm\.response\.json\(\)|JSON\.parse\(\s*responseBody\s*\))\s*;?\s*$
    )""", re.X)
_ALIAS_DECL = re.compile(r"(?:var|let|const)\s+(\w+)\s*=\s*(?:pm\.response\.json\(\)|JSON\.parse\(\s*responseBody\s*\))")
_VAR_GET = re.compile(r"""^pm\.(?:environment|collectionVariables|variables|globals|iterationData)\.get\(\s*["']([^"']+)["']\s*\)$""")
_PATH_TAIL = r"((?:\.\w+|\[\d+\]|\[\s*['\"][^'\"]+['\"]\s*\])*)"

_TO_BE = {
    "ok": 200, "success": "2xx", "created": 201, "accepted": 202, "badRequest": 400, "unauthorized": 401,
    "forbidden": 403, "notFound": 404, "rateLimited": 429, "clientError": "4xx", "serverError": "5xx",
    "error": None, "info": "1xx", "redirection": "3xx",
}
_CHAI_TYPES = {"string": "string", "number": "number", "boolean": "boolean", "array": "array",
               "object": "object", "null": "null"}


def _balanced(s: str, start: int) -> Tuple[str, int]:
    """Given s[start] == '(', returns (inner text, index after closing paren)."""
    depth, i, quote = 0, start, None
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "/" and i + 1 < len(s) and depth > 0 and s[i - 1] in "(,[ ":
            end = s.find("/", i + 1)  # regex literal
            while end != -1 and s[end - 1] == "\\":
                end = s.find("/", end + 1)
            i = end if end != -1 else i
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return s[start + 1:i], i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def _split_args(s: str) -> List[str]:
    out, depth, quote, cur = [], 0, None, []
    for c in s:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


_NO_VALUE = object()


def _literal(arg: str) -> Any:
    a = arg.strip()
    m = _VAR_GET.match(a)
    if m:
        return "{{" + m.group(1) + "}}"
    if len(a) >= 2 and a[0] == a[-1] and a[0] in "'`":
        return a[1:-1]
    if a.startswith("[") and a.endswith("]"):
        items = [_literal(x) for x in _split_args(a[1:-1])]
        return _NO_VALUE if any(x is _NO_VALUE for x in items) else items
    if a == "undefined":
        return _NO_VALUE
    try:
        return json.loads(a)
    except ValueError:
        return _NO_VALUE


def _js_path_to_jsonpath(tail: str) -> str:
    tail = re.sub(r"""\[\s*['"]([^'"]+)['"]\s*\]""", lambda m: f"['{m.group(1)}']", tail)
    return "$" + tail


class ScriptTranslator:
    """Translates common Postman test-script statements into expectations."""

    def __init__(self) -> None:
        self.expect: Dict[str, Any] = {}
        self.save: Dict[str, str] = {}
        self.untranslated: List[str] = []
        self.aliases = {"jsonData", "responseJson", "json", "body", "data", "res", "response", "resp"}

    def translate(self, lines: List[str]) -> None:
        for line in lines:
            for m in _ALIAS_DECL.finditer(line):
                self.aliases.add(m.group(1))
        for line in lines:
            stripped = line.strip()
            if _BOILERPLATE.match(line):
                continue
            handled = False
            try:
                handled = self._line(stripped)
            except ValueError:
                handled = False
            if not handled:
                self.untranslated.append(stripped)

    # ------------------------------------------------------------------

    def _json_target(self, expr: str) -> Optional[str]:
        expr = expr.strip()
        m = re.fullmatch(r"pm\.response\.json\(\)" + _PATH_TAIL, expr)
        if m:
            return _js_path_to_jsonpath(m.group(1))
        m = re.fullmatch(r"(\w+)" + _PATH_TAIL, expr)
        if m and m.group(1) in self.aliases:
            return _js_path_to_jsonpath(m.group(2))
        return None

    def _line(self, s: str) -> bool:
        handled = False
        handled |= self._legacy(s)
        handled |= self._saves(s)
        for m in re.finditer(r"pm\.response\.to\.((?:not\.)?(?:have|be))\.(\w+)", s):
            handled |= self._response_to(m, s)
        idx = s.find("pm.expect(")
        while idx != -1:
            handled |= self._expect(s, idx)
            idx = s.find("pm.expect(", idx + 1)
        return handled

    def _legacy(self, s: str) -> bool:
        ok = False
        m = re.search(r"responseCode\.code\s*===?\s*(\d{3})", s)
        if m:
            self.expect["status"] = int(m.group(1))
            ok = True
        m = re.search(r"responseTime\s*<=?\s*(\d+)", s)
        if m and "pm.expect" not in s:
            self.expect["max_time_ms"] = int(m.group(1))
            ok = True
        m = re.search(r"""responseBody\.has\(\s*["']([^"']+)["']\s*\)""", s)
        if m:
            self.expect.setdefault("body_contains", []).append(m.group(1))
            ok = True
        return ok

    def _saves(self, s: str) -> bool:
        ok = False
        for m in re.finditer(r"(?:pm\.(?:environment|collectionVariables|variables|globals)\.set|"
                             r"postman\.set(?:Environment|Global)Variable)\(", s):
            inner, _ = _balanced(s, m.end() - 1)
            args = _split_args(inner)
            if len(args) != 2:
                return False
            name = _literal(args[0])
            if not isinstance(name, str):
                return False
            value = args[1].strip()
            hm = re.fullmatch(r"""pm\.response\.headers\.get\(\s*["']([^"']+)["']\s*\)""", value)
            if hm:
                self.save[name] = f"header:{hm.group(1)}"
                ok = True
                continue
            if value in ("pm.response.code", "responseCode.code"):
                self.save[name] = "status"
                ok = True
                continue
            path = self._json_target(value)
            if path is None:
                return False
            self.save[name] = path
            ok = True
        return ok

    def _response_to(self, m: re.Match, s: str) -> bool:
        chain, what = m.group(1), m.group(2)
        negate = chain.startswith("not.")
        rest = s[m.end():]
        args: List[str] = []
        if rest.lstrip().startswith("("):
            inner, _ = _balanced(rest, rest.index("("))
            args = _split_args(inner)
        if chain.endswith("be"):
            if what in _TO_BE and _TO_BE[what] is not None and not negate:
                self.expect["status"] = _TO_BE[what]
                return True
            if what == "json" and not negate:
                self._header("Content-Type", "contains", "json")
                return True
            if what == "withBody" and not negate:
                self.expect.setdefault("json", {})  # no-op but recognised
                return True
            return False
        # have.*
        if what == "status" and args and not negate:
            v = _literal(args[0])
            if isinstance(v, int):
                self.expect["status"] = v
                return True
            if isinstance(v, str):
                code = next((st.value for st in http.HTTPStatus if st.phrase.lower() == v.lower()), None)
                if code:
                    self.expect["status"] = code
                    return True
            return False
        if what == "header" and args:
            name = _literal(args[0])
            if not isinstance(name, str):
                return False
            if negate:
                self._header(name, "exists", False)
            elif len(args) > 1 and _literal(args[1]) is not _NO_VALUE:
                self._header(name, "eq", _literal(args[1]))
            else:
                self._header(name, "exists", True)
            return True
        if what == "body" and args and not negate:
            v = _literal(args[0])
            if isinstance(v, str):
                self.expect.setdefault("body_contains", []).append(v)
                return True
            return False
        if what == "jsonBody" and args and not negate:
            p = _literal(args[0])
            if not isinstance(p, str):
                return False
            path = "$." + p
            if len(args) > 1:
                v = _literal(args[1])
                if v is _NO_VALUE:
                    return False
                self._json(path, "eq", v)
            else:
                self._json(path, "exists", True)
            return True
        return False

    def _header(self, name: str, op: str, value: Any) -> None:
        self.expect.setdefault("headers", {}).setdefault(name, {})[op] = value

    def _json(self, path: str, op: str, value: Any) -> None:
        self.expect.setdefault("json", {}).setdefault(path, {})[op] = value

    def _expect(self, s: str, idx: int) -> bool:
        target, end = _balanced(s, idx + len("pm.expect"))
        m = re.match(r"\s*\.([A-Za-z.]+)", s[end:])
        if not m:
            return False
        words = m.group(1).strip(".").split(".")
        negate = "not" in words
        method = words[-1]
        after = end + m.end()
        args: List[str] = []
        if after < len(s) and s[after:].lstrip().startswith("("):
            inner, _ = _balanced(s, after + (len(s[after:]) - len(s[after:].lstrip())))
            args = _split_args(inner)
        cond = self._condition(method, args, negate)
        if cond is None:
            return False
        op, value = cond
        t = target.strip()

        if re.fullmatch(r"pm\.response\.(code|status)|responseCode\.code", t):
            if op == "eq" and isinstance(value, int):
                self.expect["status"] = value
            elif op == "one_of":
                self.expect["status"] = value
            elif op in ("gt", "gte", "lt", "lte"):
                st = self.expect.get("status")
                st = st if isinstance(st, dict) else {}
                st[op] = value
                self.expect["status"] = st
            else:
                return False
            return True
        if re.fullmatch(r"pm\.response\.responseTime|responseTime", t):
            if op in ("lt", "lte") and isinstance(value, (int, float)):
                self.expect["max_time_ms"] = value
                return True
            return False
        if re.fullmatch(r"pm\.response\.text\(\)|responseBody", t):
            if op == "contains":
                self.expect.setdefault("body_contains", []).append(value)
                return True
            return False
        hm = re.fullmatch(r"""pm\.response\.headers\.get\(\s*["']([^"']+)["']\s*\)""", t)
        if hm:
            self._header(hm.group(1), op, value)
            return True
        path = self._json_target(t)
        if path is None:
            return False
        if method == "property":
            prop = _literal(args[0]) if args else _NO_VALUE
            if not isinstance(prop, str):
                return False
            path = path + "." + prop if re.fullmatch(r"\w+", prop) else f"{path}['{prop}']"
            if len(args) > 1:
                v = _literal(args[1])
                if v is _NO_VALUE:
                    return False
                self._json(path, "ne" if negate else "eq", v)
            else:
                self._json(path, "exists", not negate)
            return True
        self._json(path, op, value)
        return True

    def _condition(self, method: str, args: List[str], negate: bool) -> Optional[Tuple[str, Any]]:
        arg = _literal(args[0]) if args else _NO_VALUE
        if method in ("eql", "equal", "eq", "equals", "eqls"):
            return None if arg is _NO_VALUE else ("ne" if negate else "eq", arg)
        simple = {"above": "gt", "greaterThan": "gt", "gt": "gt", "below": "lt", "lessThan": "lt", "lt": "lt",
                  "least": "gte", "gte": "gte", "most": "lte", "lte": "lte"}
        if method in simple and not negate:
            return None if not isinstance(arg, (int, float)) else (simple[method], arg)
        if method in ("include", "includes", "contain", "contains", "string"):
            return None if arg is _NO_VALUE else ("not_contains" if negate else "contains", arg)
        if method == "oneOf" and not negate:
            return None if not isinstance(arg, list) else ("one_of", arg)
        if method in ("lengthOf", "length") and not negate:
            return None if not isinstance(arg, int) else ("len", arg)
        if method == "match" and args and not negate:
            rm = re.fullmatch(r"/(.*)/([a-z]*)", args[0].strip())
            if not rm:
                return None
            flags = "(?i)" if "i" in rm.group(2) else ""
            return ("regex", flags + rm.group(1))
        if method in ("a", "an") and not negate:
            return None if arg not in _CHAI_TYPES else ("type", _CHAI_TYPES[arg])
        if method == "exist":
            return ("exists", not negate)
        if method == "undefined":
            return ("exists", negate)
        if method == "empty":
            return ("not_empty", True) if negate else ("len", 0)
        if method in ("true", "false", "null"):
            v = {"true": True, "false": False, "null": None}[method]
            return ("ne" if negate else "eq", v)
        if method == "property":
            return ("property", None)
        return None
