"""``{{variable}}`` / ``{{$dynamic}}`` substitution for request and expectation templates.

A string that is exactly one placeholder (``"{{userId}}"``) keeps the value's type,
so ints, booleans, lists and objects stay typed inside JSON bodies.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .datagen import DataGen

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_MAX_DEPTH = 5


class UnresolvedVariable(KeyError):
    def __init__(self, name: str, hint: Optional[str] = None):
        self.name = name
        self.hint = hint
        super().__init__(name)

    def __str__(self) -> str:
        msg = f"Variable '{{{{{self.name}}}}}' is not defined."
        if self.hint:
            msg += " " + self.hint
        else:
            msg += " Define it in the environment file, the suite's 'variables', with --var, or save it from an earlier step."
        return msg


class Renderer:
    def __init__(self, scope: Mapping[str, Any], datagen: DataGen, hints: Optional[Mapping[str, str]] = None):
        self.scope = scope
        self.datagen = datagen
        self.hints = hints or {}

    def render(self, value: Any, _depth: int = 0) -> Any:
        if isinstance(value, str):
            return self._render_str(value, _depth)
        if isinstance(value, dict):
            return {self.render(k, _depth) if isinstance(k, str) else k: self.render(v, _depth) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v, _depth) for v in value]
        return value

    def _lookup(self, name: str, depth: int) -> Any:
        if name.startswith("$"):
            return self.datagen.resolve(name)
        if name not in self.scope:
            hint = self.hints.get(name)
            raise UnresolvedVariable(name, f"It is saved by step '{hint}' - did that step fail or get deselected?" if hint else None)
        value = self.scope[name]
        if isinstance(value, (str, dict, list)) and depth < _MAX_DEPTH and has_placeholders(value):
            try:  # variables may reference other variables, e.g. baseUrl: "{{host}}/api"
                value = self.render(value, depth + 1)
            except (UnresolvedVariable, ValueError):
                pass  # leave e.g. saved response text containing literal braces untouched
        return value

    def _render_str(self, s: str, depth: int) -> Any:
        whole = _PLACEHOLDER.fullmatch(s)
        if whole:
            return self._lookup(whole.group(1), depth)
        if "{{" not in s:
            return s
        return _PLACEHOLDER.sub(lambda m: to_text(self._lookup(m.group(1), depth)), s)


def to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (bool, dict, list)):
        return json.dumps(v)
    return str(v)


def has_placeholders(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_PLACEHOLDER.search(value))
    if isinstance(value, dict):
        return any(has_placeholders(k) or has_placeholders(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_placeholders(v) for v in value)
    return False


def placeholder_names(value: Any) -> set:
    """All ``{{name}}`` names referenced anywhere in ``value``."""
    out: set = set()
    if isinstance(value, str):
        out.update(m.group(1).strip() for m in _PLACEHOLDER.finditer(value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out |= placeholder_names(k) | placeholder_names(v)
    elif isinstance(value, list):
        for v in value:
            out |= placeholder_names(v)
    return out
