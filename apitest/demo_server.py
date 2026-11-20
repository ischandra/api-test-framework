"""A small in-memory REST API used by the examples and the framework's self-tests.

    python -m apitest demo-server --port 8000

Endpoints:
  GET    /health
  POST   /auth/login        {"username": "demo", "password": "demo-pass"} -> {"token": ...}
  GET    /users?limit=N
  POST   /users             (Bearer token) {"name", "email", "age"?} -> 201 / 400 / 401 / 409
  GET    /users/{id}        -> 200 / 404
  PUT    /users/{id}        (Bearer token)
  DELETE /users/{id}        (Bearer token) -> 204
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

USERNAME, PASSWORD = "demo", "demo-pass"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users: Dict[int, Dict[str, Any]] = {}
        self.next_id = 1
        self.tokens = set()


def _validate(body: Any, partial: bool = False) -> Dict[str, str]:
    errors: Dict[str, str] = {}
    if not isinstance(body, dict):
        return {"body": "must be a JSON object"}
    if not partial or "name" in body:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            errors["name"] = "is required"
        elif len(name) > 100:
            errors["name"] = "must be at most 100 characters"
    if not partial or "email" in body:
        email = body.get("email")
        if not isinstance(email, str) or not _EMAIL.match(email):
            errors["email"] = "must be a valid email address"
    if "age" in body and body["age"] is not None:
        age = body["age"]
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 150:
            errors["age"] = "must be an integer between 0 and 150"
    return errors


def make_handler(store: _Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DemoAPI/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep test output quiet
            pass

        # ------------------------------------------------------------ utils
        def _send(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if payload and self.command != "HEAD":
                self.wfile.write(payload)

        def _body(self) -> Tuple[bool, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return True, None
            try:
                return True, json.loads(raw)
            except ValueError:
                return False, None

        def _authorized(self) -> bool:
            h = self.headers.get("Authorization", "")
            return h.startswith("Bearer ") and h[7:] in store.tokens

        def _route(self) -> Tuple[str, Optional[int], Dict[str, list]]:
            parts = urlsplit(self.path)
            m = re.fullmatch(r"/users/(\d+)", parts.path)
            return (("/users/{id}", int(m.group(1))) if m else (parts.path.rstrip("/") or "/", None)) + (parse_qs(parts.query),)

        # ---------------------------------------------------------- verbs
        def do_GET(self) -> None:
            route, uid, q = self._route()
            if route == "/health":
                return self._send(200, {"status": "UP", "time": datetime.now(timezone.utc).isoformat()})
            if route == "/users":
                limit = int((q.get("limit") or ["50"])[0])
                with store.lock:
                    items = list(store.users.values())
                return self._send(200, {"items": items[:limit], "total": len(items)})
            if route == "/users/{id}":
                with store.lock:
                    user = store.users.get(uid)
                return self._send(200, user) if user else self._send(404, {"error": "not found"})
            self._send(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_POST(self) -> None:
            route, _, _ = self._route()
            ok, body = self._body()
            if not ok:
                return self._send(400, {"error": "invalid JSON"})
            if route == "/auth/login":
                if isinstance(body, dict) and body.get("username") == USERNAME and body.get("password") == PASSWORD:
                    token = secrets.token_hex(16)
                    store.tokens.add(token)
                    return self._send(200, {"token": token, "tokenType": "Bearer", "expiresIn": 3600})
                return self._send(401, {"error": "invalid credentials"})
            if route == "/users":
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                errors = _validate(body)
                if errors:
                    return self._send(400, {"error": "validation failed", "details": errors})
                with store.lock:
                    if any(u["email"].lower() == body["email"].lower() for u in store.users.values()):
                        return self._send(409, {"error": "email already exists"})
                    uid = store.next_id
                    store.next_id += 1
                    user = {"id": uid, "name": body["name"], "email": body["email"], "age": body.get("age"),
                            "createdAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
                    store.users[uid] = user
                return self._send(201, user, {"Location": f"/users/{uid}"})
            self._send(404, {"error": "not found"})

        def do_PUT(self) -> None:
            route, uid, _ = self._route()
            if route != "/users/{id}":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            ok, body = self._body()
            if not ok:
                return self._send(400, {"error": "invalid JSON"})
            errors = _validate(body, partial=True)
            if errors:
                return self._send(400, {"error": "validation failed", "details": errors})
            with store.lock:
                user = store.users.get(uid)
                if not user:
                    return self._send(404, {"error": "not found"})
                user.update({k: body[k] for k in ("name", "email", "age") if k in body})
            self._send(200, user)

        def do_DELETE(self) -> None:
            route, uid, _ = self._route()
            if route != "/users/{id}":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            with store.lock:
                if store.users.pop(uid, None) is None:
                    return self._send(404, {"error": "not found"})
            self._send(204)

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(_Store()))
    server.daemon_threads = True
    return server


def start_in_thread(host: str = "127.0.0.1", port: int = 0) -> Tuple[ThreadingHTTPServer, str]:
    """Starts the demo API on a background thread; returns (server, base_url)."""
    server = create_server(host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = create_server(host, port)
    print(f"Demo API listening on http://{host}:{server.server_address[1]}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
