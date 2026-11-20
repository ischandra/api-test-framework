"""HTTP client wrapper plus secret-masked request/response formatting for reports."""
from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger("apitest")

_SECRET_NAME = re.compile(r"(?i)(authorization|cookie|token|secret|passw(or)?d|api[-_]?key|credential|session)")
MASK = "***"
_NOISE_HEADERS = {"content-length", "accept-encoding", "connection", "user-agent"}
_BODY_LIMIT = 3000


class ApiClient:
    """Thin wrapper over :class:`requests.Session` with base-URL joining and auth helpers.

    ``auth`` forms: ``{"bearer": token}``, ``{"basic": {"username": u, "password": p}}``,
    ``{"api_key": {"name": "X-API-Key", "value": v, "in": "header"|"query"}}``.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30, verify: bool = True,
                 headers: Optional[Dict[str, str]] = None):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        if headers:
            self.session.headers.update(headers)

    def url_for(self, url: str) -> str:
        if re.match(r"(?i)^https?://", url):
            return url
        if not self.base_url:
            raise ValueError(f"'{url}' is relative but no base URL is set. Set baseUrl in the environment "
                             "file, use --base-url, or give an absolute URL.")
        return self.base_url.rstrip("/") + "/" + url.lstrip("/")

    def request(self, method: str, url: str, *, headers: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, Any]] = None, json_body: Any = None, has_json: bool = False,
                body: Optional[str] = None, form: Optional[Dict[str, Any]] = None,
                multipart: Optional[Dict[str, Any]] = None, auth: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> requests.Response:
        hdrs = {k: _text(v) for k, v in (headers or {}).items()}
        query = {k: _text(v) for k, v in (params or {}).items()}
        req_auth = None
        if auth:
            if "bearer" in auth:
                hdrs.setdefault("Authorization", f"Bearer {_text(auth['bearer'])}")
            elif "basic" in auth:
                b = auth["basic"]
                req_auth = (_text(b.get("username")), _text(b.get("password")))
            elif "api_key" in auth:
                k = auth["api_key"]
                target = query if str(k.get("in", "header")).lower() == "query" else hdrs
                target.setdefault(_text(k.get("name", "X-API-Key")), _text(k.get("value")))
            else:
                raise ValueError(f"Unsupported auth {list(auth)}; use bearer, basic or api_key")

        kwargs: Dict[str, Any] = {"headers": hdrs, "params": query or None, "auth": req_auth,
                                  "timeout": timeout or self.timeout}
        if has_json:
            kwargs["json"] = json_body
        elif body is not None:
            kwargs["data"] = body.encode("utf-8")
        elif form is not None:
            kwargs["data"] = {k: _text(v) for k, v in form.items()}
        elif multipart is not None:
            kwargs["files"] = {k: (None, _text(v)) for k, v in multipart.items()}

        full_url = self.url_for(url)
        log.info("-> %s %s", method.upper(), mask_url(full_url))
        resp = self.session.request(method.upper(), full_url, **kwargs)
        log.info("<- %s %s (%.0f ms)", resp.status_code, resp.reason, resp.elapsed.total_seconds() * 1000)
        return resp

    def get(self, url: str, **kw: Any) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("POST", url, json_body=json, has_json=json is not None, **kw)

    def put(self, url: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("PUT", url, json_body=json, has_json=json is not None, **kw)

    def patch(self, url: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("PATCH", url, json_body=json, has_json=json is not None, **kw)

    def delete(self, url: str, **kw: Any) -> requests.Response:
        return self.request("DELETE", url, **kw)

    def close(self) -> None:
        self.session.close()


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


# ------------------------------------------------------------------ masking

def mask_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, MASK if _SECRET_NAME.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(q, safe="*")))


def mask_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if isinstance(k, str) and _SECRET_NAME.search(k) and not isinstance(v, (dict, list))
                    else mask_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_json(v) for v in value]
    return value


def _mask_headers(headers) -> Dict[str, str]:
    return {k: (MASK if _SECRET_NAME.search(k) else v) for k, v in headers.items()}


def _format_body(raw: Any, content_type: str) -> str:
    if raw is None or raw == b"" or raw == "":
        return ""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(raw)} bytes of binary data>"
    text = str(raw)
    try:
        text = json.dumps(mask_json(json.loads(text)), indent=2, ensure_ascii=False)
    except ValueError:
        if "x-www-form-urlencoded" in content_type:
            text = urlencode([(k, MASK if _SECRET_NAME.search(k) else v)
                              for k, v in parse_qsl(text, keep_blank_values=True)], safe="*")
    if len(text) > _BODY_LIMIT:
        text = text[:_BODY_LIMIT] + f"\n... ({len(text) - _BODY_LIMIT} more chars truncated)"
    return text


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def describe_exchange(resp: Optional[requests.Response] = None, request: Optional[requests.PreparedRequest] = None) -> str:
    """Human-readable request + response with secrets masked, plus a curl reproduction."""
    req = request if request is not None else (resp.request if resp is not None else None)
    lines = []
    if req is not None:
        ct = req.headers.get("Content-Type", "")
        lines.append("Request:")
        lines.append(f"    {req.method} {mask_url(req.url)}")
        for k, v in _mask_headers(req.headers).items():
            if k.lower() not in _NOISE_HEADERS:
                lines.append(f"    {k}: {v}")
        body = _format_body(req.body, ct)
        if body:
            lines.append(_indent(body))
    if resp is not None:
        lines.append("")
        lines.append(f"Response: {resp.status_code} {resp.reason} ({resp.elapsed.total_seconds() * 1000:.0f} ms)")
        for k, v in _mask_headers(resp.headers).items():
            lines.append(f"    {k}: {v}")
        body = _format_body(resp.content, resp.headers.get("Content-Type", ""))
        if body:
            lines.append(_indent(body))
    if req is not None:
        lines.append("")
        lines.append("Reproduce (secrets masked):")
        lines.append("    " + to_curl(req))
    return "\n".join(lines)


def to_curl(req: requests.PreparedRequest) -> str:
    parts = ["curl", "-X", req.method or "GET", shlex.quote(mask_url(req.url or ""))]
    for k, v in _mask_headers(req.headers).items():
        if k.lower() not in _NOISE_HEADERS:
            parts += ["-H", shlex.quote(f"{k}: {v}")]
    if req.body:
        body = req.body.decode("utf-8", "replace") if isinstance(req.body, bytes) else str(req.body)
        try:
            body = json.dumps(mask_json(json.loads(body)), ensure_ascii=False)
        except ValueError:
            pass
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT] + "..."
        parts += ["--data-raw", shlex.quote(body)]
    return " ".join(parts)
