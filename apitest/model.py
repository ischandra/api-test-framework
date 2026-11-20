"""In-memory representation of a test suite, shared by all loaders."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


@dataclass
class CaseSpec:
    name: str
    method: str
    url: str                                   # absolute URL or path joined to the suite base URL
    headers: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    json: Any = None                           # JSON body (None = not sent)
    has_json: bool = False
    body: Optional[str] = None                 # raw text body
    form: Optional[Dict[str, Any]] = None      # application/x-www-form-urlencoded
    multipart: Optional[Dict[str, Any]] = None # multipart/form-data text fields
    auth: Optional[Dict[str, Any]] = None
    timeout: Optional[float] = None
    expect: Dict[str, Any] = field(default_factory=dict)
    save: Dict[str, str] = field(default_factory=dict)
    variables: Dict[str, Any] = field(default_factory=dict)  # per-case data (cases / data_file)
    tags: List[str] = field(default_factory=list)
    skip: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class SuiteSpec:
    name: str
    source: Path
    kind: str                                  # "yaml" | "postman"
    base_url: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    cases: List[CaseSpec] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def base_dir(self) -> Path:
        return self.source.parent

    def save_sources(self) -> Dict[str, str]:
        """Variable name -> name of the step that saves it (for helpful errors)."""
        out: Dict[str, str] = {}
        for c in self.cases:
            for var in c.save:
                out.setdefault(var, c.name)
        return out
