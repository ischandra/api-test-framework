"""Command line: ``python -m apitest <command>``.

  run             Test API URLs directly, YAML suites, or Postman collections (file or URL)
  import-postman  Convert a Postman collection into an editable YAML suite
  demo-server     Start the bundled demo API (for trying things out)
  dynamic-vars    List the {{$...}} synthetic data variables
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import yaml

_URL = re.compile(r"(?i)^https?://")
_METHOD_URL = re.compile(r"(?i)^(get|post|put|patch|delete|head|options)\s+(https?://\S+)$")
TMP_DIR = Path(".apitest-tmp")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apitest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run API tests", description=(
        "Targets can be: API URLs (smoke-tested, e.g. https://host/health or 'POST https://host/x'), "
        "*.api.yaml suites, Postman collection files, or directories. Anything after '--' goes to pytest."))
    run.add_argument("targets", nargs="*", help="URLs, suite/collection files or directories")
    run.add_argument("-c", "--collection", action="append", default=[],
                     help="Postman collection file or URL to download and run (repeatable)")
    run.add_argument("-e", "--env", help="environment name or file")
    run.add_argument("--postman-env", action="append", default=[], help="Postman environment JSON (repeatable)")
    run.add_argument("--base-url", help="override baseUrl")
    run.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--seed", type=int)
    run.add_argument("--expect-status", default="2xx", help="expected status for direct URLs (default 2xx)")
    run.add_argument("--max-time-ms", type=int, help="max response time for direct URLs")
    run.add_argument("-H", "--header", action="append", default=[], help="'Name: value' header for direct URLs")
    run.add_argument("--junit", default="reports/junit.xml", help="JUnit XML report path ('' to disable)")
    run.add_argument("-k", dest="keyword", help="only run tests matching this pytest -k expression")
    run.add_argument("-m", dest="markers", help="only run tests with these tags, e.g. 'smoke and not slow'")

    imp = sub.add_parser("import-postman", help="convert a Postman collection to a YAML suite")
    imp.add_argument("collection", help="collection file or URL")
    imp.add_argument("-o", "--output", help="output file (default suites/<name>.api.yaml)")

    demo = sub.add_parser("demo-server", help="start the bundled demo API")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)

    sub.add_parser("dynamic-vars", help="list {{$...}} synthetic data variables")

    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    args = parser.parse_args(argv)

    if args.cmd == "demo-server":
        from .demo_server import serve
        serve(args.host, args.port)
        return 0
    if args.cmd == "dynamic-vars":
        from .datagen import DataGen
        g = DataGen(1)
        for name in g.names():
            try:
                example = g.resolve("$" + name)
            except Exception:  # parametrised helpers need args
                example = "(needs arguments)"
            print(f"  {{{{${name}}}}}".ljust(30), repr(example)[:60])
        return 0
    if args.cmd == "import-postman":
        return _import(args)
    return _run(args, passthrough)


def _fetch(url: str, dest_dir: Path) -> Path:
    import requests

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    doc = resp.json()
    if "collection" in doc and "item" not in doc:  # Postman API wraps the collection
        doc = doc["collection"]
    name = re.sub(r"[^\w.-]+", "_", (doc.get("info") or {}).get("name") or "downloaded")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{name}.postman_collection.json"
    import json
    out.write_text(json.dumps(doc), encoding="utf-8")
    return out


def _run(args: argparse.Namespace, passthrough: List[str]) -> int:
    import pytest

    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    paths: List[str] = []
    smoke = []
    for t in args.targets:
        m = _METHOD_URL.match(t.strip())
        if m:
            smoke.append({"method": m.group(1).upper(), "url": m.group(2)})
        elif _URL.match(t):
            smoke.append(t)
        else:
            paths.append(t)
    for c in args.collection:
        paths.append(str(_fetch(c, TMP_DIR)) if _URL.match(c) else c)

    if smoke:
        headers = {}
        for h in args.header:
            name, _, value = h.partition(":")
            headers[name.strip()] = value.strip()
        expect = {"status": _status(args.expect_status)}
        if args.max_time_ms:
            expect["max_time_ms"] = args.max_time_ms
        suite = {"name": "Direct URLs", "base_url": None, "defaults": {"headers": headers, "expect": expect},
                 "smoke": smoke}
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        f = TMP_DIR / "direct-urls.api.yaml"
        f.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
        paths.append(str(f))

    pytest_args = ["-p", "apitest.plugin", "-rA" if smoke and not paths[:-1] else "-ra", *paths]
    if args.env:
        pytest_args += ["--env", args.env]
    for p in args.postman_env:
        pytest_args += ["--postman-env", p]
    if args.base_url:
        pytest_args += ["--base-url", args.base_url]
    for v in args.var:
        pytest_args += ["--var", v]
    if args.seed is not None:
        pytest_args += ["--seed", str(args.seed)]
    if args.junit:
        pytest_args += [f"--junitxml={args.junit}"]
    if args.keyword:
        pytest_args += ["-k", args.keyword]
    if args.markers:
        pytest_args += ["-m", args.markers]
    return int(pytest.main(pytest_args + passthrough))


def _status(s: str):
    if "," in s:
        return [int(x) for x in s.split(",")]
    return int(s) if s.isdigit() else s


def _import(args: argparse.Namespace) -> int:
    from .loaders import postman, yaml_suite

    src = _fetch(args.collection, TMP_DIR) if _URL.match(args.collection) else Path(args.collection)
    suite = postman.load(src)
    out = Path(args.output) if args.output else Path("suites") / (re.sub(r"[^\w-]+", "-", suite.name).strip("-").lower() + ".api.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_suite.dump(suite), encoding="utf-8")
    warned = sum(1 for c in suite.cases if c.warnings)
    print(f"Wrote {out} ({len(suite.cases)} request(s)"
          f"{f', {warned} with notes to review' if warned else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
