"""Minimal JSONPath: ``$.a.b``, ``$.items[0]``, ``$.items[-1]``, ``$.items[*].id``,
``$['key with space']`` and a JS-style ``.length`` on arrays/strings."""
from __future__ import annotations

import re
from typing import Any, List, Tuple

_TOKEN = re.compile(r"""\.([A-Za-z_$@][\w$@-]*)|\[(-?\d+|\*)\]|\[['"]([^'"]+)['"]\]""")


class PathNotFound(KeyError):
    def __init__(self, path: str):
        self.path = path
        super().__init__(path)

    def __str__(self) -> str:
        return f"path {self.path} not found in response"


class InvalidPath(ValueError):
    pass


def parse(path: str) -> List[Tuple[str, Any]]:
    p = path.strip()
    if p in ("", "$"):
        return []
    if p.startswith("$"):
        p = p[1:]
    elif not p.startswith((".", "[")):
        p = "." + p
    tokens, pos = [], 0
    while pos < len(p):
        m = _TOKEN.match(p, pos)
        if not m:
            raise InvalidPath(f"Invalid JSON path '{path}' near '{p[pos:]}'")
        if m.group(1) is not None:
            tokens.append(("key", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("wild", None) if m.group(2) == "*" else ("index", int(m.group(2))))
        else:
            tokens.append(("key", m.group(3)))
        pos = m.end()
    return tokens


def extract(doc: Any, path: str) -> Any:
    """Returns the value at ``path``; raises :class:`PathNotFound` when absent.
    A ``[*]`` segment returns a list of the matches."""
    return _walk(doc, parse(path), path)


def exists(doc: Any, path: str) -> bool:
    try:
        extract(doc, path)
        return True
    except PathNotFound:
        return False


def _walk(node: Any, tokens: List[Tuple[str, Any]], path: str) -> Any:
    if not tokens:
        return node
    kind, key = tokens[0]
    rest = tokens[1:]
    if kind == "wild":
        if isinstance(node, list):
            items = node
        elif isinstance(node, dict):
            items = list(node.values())
        else:
            raise PathNotFound(path)
        out = []
        for it in items:
            try:
                out.append(_walk(it, rest, path))
            except PathNotFound:
                pass
        return out
    if kind == "index":
        if isinstance(node, list) and -len(node) <= key < len(node):
            return _walk(node[key], rest, path)
        raise PathNotFound(path)
    if isinstance(node, dict) and key in node:
        return _walk(node[key], rest, path)
    if key == "length" and isinstance(node, (list, str)):
        return _walk(len(node), rest, path)
    raise PathNotFound(path)
