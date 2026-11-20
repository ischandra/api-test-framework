"""pytest plugin: collects ``*.api.yaml`` suites and Postman collections as tests,
and provides ``api`` / ``data`` / ``api_vars`` fixtures for code-based tests."""
from __future__ import annotations

import json
import os
import re
from collections import ChainMap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests
import yaml

from . import jsonpath
from .assertions import check_response
from .client import ApiClient, describe_exchange
from .datagen import DataGen, UnknownDynamicVariable
from .loaders import postman, yaml_suite
from .model import CaseSpec, SuiteSpec
from .templating import Renderer, UnresolvedVariable, placeholder_names

CTX_KEY = pytest.StashKey["RunContext"]()
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ApiTestWarning(pytest.PytestWarning):
    """Something in a suite/collection could not be fully honoured."""


class ApiFailure(Exception):
    """Raised for an API expectation failure."""

    def __init__(self, failures: List[str]):
        self.failures = failures
        super().__init__("; ".join(failures))


# =============================================================== options

def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("apitest", "API testing")
    g.addoption("--env", dest="api_env", default=os.environ.get("API_ENV"),
                help="Environment name (environments/<name>.yaml) or path to an env file "
                     "(.yaml, or a Postman environment .json). Default: $API_ENV, else 'local' if present.")
    g.addoption("--base-url", dest="api_base_url", help="Override the baseUrl variable.")
    g.addoption("--var", dest="api_vars", action="append", default=[], metavar="NAME=VALUE",
                help="Set/override a variable (repeatable). Highest precedence.")
    g.addoption("--postman-env", dest="api_postman_env", action="append", default=[],
                help="Postman environment/globals export to load (repeatable).")
    g.addoption("--seed", dest="api_seed", type=int, default=None,
                help="Seed for synthetic data (printed in the header; reuse it to reproduce a run).")
    g.addoption("--api-timeout", dest="api_timeout", type=float, default=30.0, help="Request timeout in seconds.")
    g.addoption("--api-insecure", dest="api_insecure", action="store_true", help="Skip TLS certificate verification.")


# =============================================================== run context

class RunContext:
    def __init__(self, config: pytest.Config):
        self.rootdir = Path(config.rootpath)
        self.datagen = DataGen(config.getoption("api_seed"))
        self.timeout = config.getoption("api_timeout")
        self.verify = not config.getoption("api_insecure")
        self.missing_env: List[str] = []
        self.env_name, self.env_file = self._find_env(config.getoption("api_env"))
        self.env_vars: Dict[str, Any] = self._load_env(self.env_file) if self.env_file else {}
        self.postman_env: Dict[str, Any] = {}
        for p in config.getoption("api_postman_env"):
            self.postman_env.update(postman.load_environment(Path(p)))
        self.cli_vars: Dict[str, Any] = {}
        for item in config.getoption("api_vars"):
            if "=" not in item:
                raise pytest.UsageError(f"--var expects NAME=VALUE, got '{item}'")
            k, v = item.split("=", 1)
            self.cli_vars[k.strip()] = _coerce(v)
        if config.getoption("api_base_url"):
            self.cli_vars["baseUrl"] = config.getoption("api_base_url")

    def _find_env(self, name: Optional[str]):
        if not name:
            for ext in ("yaml", "yml"):
                p = self.rootdir / "environments" / f"local.{ext}"
                if p.is_file():
                    return "local", p
            return None, None
        p = Path(name)
        if p.is_file():
            return p.stem, p
        for ext in ("yaml", "yml", "json"):
            p = self.rootdir / "environments" / f"{name}.{ext}"
            if p.is_file():
                return name, p
        raise pytest.UsageError(f"Environment '{name}' not found (looked for environments/{name}.yaml|yml|json)")

    def _load_env(self, path: Path) -> Dict[str, Any]:
        if path.suffix.lower() == ".json":
            return postman.load_environment(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise pytest.UsageError(f"{path}: environment file must be a mapping of variables")

        def sub(v: Any) -> Any:
            if isinstance(v, str):
                def repl(m: re.Match) -> str:
                    val = os.environ.get(m.group(1))
                    if val is None:
                        if m.group(2) is not None:
                            return m.group(2)
                        self.missing_env.append(m.group(1))
                        return ""
                    return val
                return _ENV_REF.sub(repl, v)
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v
        return sub(data)

    def global_scope(self) -> Dict[str, Any]:
        return {**self.env_vars, **self.postman_env, **self.cli_vars}


def _coerce(v: str) -> Any:
    try:
        return json.loads(v)
    except ValueError:
        return v


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "api: test generated from an API suite or Postman collection")
    config.stash[CTX_KEY] = RunContext(config)


def pytest_report_header(config: pytest.Config) -> List[str]:
    ctx = config.stash[CTX_KEY]
    scope = ctx.global_scope()
    lines = [f"apitest: env={ctx.env_name or '-'}"
             f"{f' ({ctx.env_file})' if ctx.env_file else ''}  baseUrl={scope.get('baseUrl', '-')}  seed={ctx.datagen.seed}"]
    if ctx.missing_env:
        lines.append(f"apitest: WARNING unset OS environment variables: {', '.join(sorted(set(ctx.missing_env)))}")
    return lines


# =============================================================== collection

@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: List[pytest.Item]):
    """When -k / -m deselects steps that selected steps depend on (e.g. Login saving
    {{token}}), put those prerequisite steps back so the selected tests can run."""
    original = list(items)
    yield
    selected = {id(i) for i in items}
    glob = config.stash[CTX_KEY].global_scope()
    added: List[pytest.Item] = []
    by_suite: Dict[int, List[ApiItem]] = {}
    for it in original:
        if isinstance(it, ApiItem):
            by_suite.setdefault(id(it.parent), []).append(it)

    def require(item: "ApiItem") -> None:
        steps = by_suite[id(item.parent)]
        idx = steps.index(item)
        for var in item.needed_vars():
            if var in glob:
                continue
            producer = next((s for s in reversed(steps[:idx]) if var in s.case.save), None)
            if producer is not None and id(producer) not in selected:
                selected.add(id(producer))
                added.append(producer)
                require(producer)

    for it in list(items):
        if isinstance(it, ApiItem):
            require(it)
    if added:
        items[:] = [i for i in original if id(i) in selected]
        tr = config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None:
            tr.write_line(f"apitest: added {len(added)} prerequisite step(s) needed by the selected tests: "
                          + ", ".join(a.name for a in added))


def pytest_collect_file(file_path: Path, parent: pytest.Collector):
    name = file_path.name
    if name.endswith((".api.yaml", ".api.yml")):
        return SuiteFile.from_parent(parent, path=file_path)
    if name.endswith(".json") and postman.is_collection(file_path):
        return SuiteFile.from_parent(parent, path=file_path)
    return None


class SuiteState:
    """Variables for one suite run: suite variables (rendered once) + values saved by steps."""

    def __init__(self, suite: SuiteSpec, ctx: RunContext):
        self.suite = suite
        self.ctx = ctx
        self.saved: Dict[str, Any] = {}
        self.hints = suite.save_sources()
        glob = ctx.global_scope()
        rendered: Dict[str, Any] = {}
        renderer = Renderer(ChainMap(glob, rendered), ctx.datagen, self.hints)
        for k, v in suite.variables.items():
            if k not in glob:
                rendered[k] = renderer.render(v)
        self.suite_vars = rendered
        self.client = ApiClient(timeout=ctx.timeout, verify=ctx.verify)

    def scope(self, case_vars: Dict[str, Any]) -> ChainMap:
        c = self.ctx
        return ChainMap(c.cli_vars, case_vars, self.saved, c.postman_env, c.env_vars, self.suite_vars)


class SuiteFile(pytest.File):
    def collect(self):
        try:
            spec = postman.load(self.path) if self.path.suffix == ".json" else yaml_suite.load(self.path)
        except (yaml_suite.SuiteError, postman.PostmanError) as e:
            raise self.CollectError(str(e)) from e
        self.spec = spec
        self._state: Optional[SuiteState] = None
        for case in spec.cases:
            item = ApiItem.from_parent(self, name=case.name, case=case)
            item.add_marker(pytest.mark.api)
            for tag in case.tags:
                tag = re.sub(r"\W", "_", str(tag))
                self.config.addinivalue_line("markers", f"{tag}: API suite tag")
                item.add_marker(getattr(pytest.mark, tag))
            if case.skip:
                item.add_marker(pytest.mark.skip(reason=case.skip))
            if case.warnings:
                item.warn(ApiTestWarning(f"{spec.name} :: {case.name}: " + "; ".join(case.warnings)))
            yield item

    def state(self) -> SuiteState:
        if self._state is None:
            self._state = SuiteState(self.spec, self.config.stash[CTX_KEY])
        return self._state

    def teardown(self) -> None:
        if getattr(self, "_state", None) is not None:
            self._state.client.close()


class ApiItem(pytest.Item):
    def __init__(self, *, case: CaseSpec, **kwargs: Any):
        super().__init__(**kwargs)
        self.case = case
        self._resp: Optional[requests.Response] = None
        self._prepared: Optional[requests.PreparedRequest] = None

    def needed_vars(self) -> set:
        c = self.case
        names = placeholder_names([c.url, c.headers, c.params, c.json, c.body, c.form, c.multipart, c.auth,
                                   c.expect, c.variables, self.parent.spec.base_url])
        # suite variables referenced indirectly
        for k in list(names):
            if k in self.parent.spec.variables:
                names |= placeholder_names(self.parent.spec.variables[k])
        return {n for n in names if not n.startswith("$")} - set(c.variables)

    def runtest(self) -> None:
        state: SuiteState = self.parent.state()
        ctx = state.ctx
        case = self.case
        pre = Renderer(state.scope({}), ctx.datagen, state.hints)
        case_vars = pre.render(case.variables) if case.variables else {}
        r = Renderer(state.scope(case_vars), ctx.datagen, state.hints)

        base_url = r.render(state.suite.base_url) if state.suite.base_url else None
        if base_url == "":
            base_url = None
        state.client.base_url = base_url
        url = r.render(case.url)
        try:
            resp = state.client.request(
                case.method, str(url),
                headers=r.render(case.headers), params=r.render(case.params),
                json_body=r.render(case.json) if case.has_json else None, has_json=case.has_json,
                body=_as_text(r.render(case.body)) if case.body is not None else None,
                form=r.render(case.form) if case.form is not None else None,
                multipart=r.render(case.multipart) if case.multipart is not None else None,
                auth=r.render(case.auth) if case.auth else None,
                timeout=case.timeout,
            )
        except requests.RequestException as e:
            self._prepared = getattr(e, "request", None)
            raise ApiFailure([f"request failed: {e.__class__.__name__}: {e}"]) from e
        self._resp = resp
        self.user_properties += [("method", case.method), ("url", resp.request.url),
                                 ("status", resp.status_code),
                                 ("elapsed_ms", round(resp.elapsed.total_seconds() * 1000))]

        failures = check_response(resp, r.render(case.expect), state.suite.base_dir)
        if not failures:
            failures = self._save(resp, state)
        if failures:
            raise ApiFailure(failures)

    def _save(self, resp: requests.Response, state: SuiteState) -> List[str]:
        errors = []
        for var, source in self.case.save.items():
            src = source.strip()
            try:
                if src == "status":
                    state.saved[var] = resp.status_code
                elif src == "body":
                    state.saved[var] = resp.text
                elif src.lower().startswith("header:"):
                    h = src.split(":", 1)[1].strip()
                    if h not in resp.headers:
                        raise jsonpath.PathNotFound(f"header {h}")
                    state.saved[var] = resp.headers[h]
                else:
                    state.saved[var] = jsonpath.extract(resp.json(), src)
            except (jsonpath.PathNotFound, ValueError) as e:
                errors.append(f"save {var}: cannot read {src} from response ({e})")
        return errors

    def repr_failure(self, excinfo, style=None):
        exc = excinfo.value
        ctx: RunContext = self.config.stash[CTX_KEY]
        rerun = f"pytest '{self.path.relative_to(self.config.rootpath) if self.path.is_relative_to(self.config.rootpath) else self.path}'" \
                f"{' --env ' + ctx.env_name if ctx.env_name else ''} --seed {ctx.datagen.seed}"
        if isinstance(exc, ApiFailure):
            n = len(exc.failures)
            parts = [exc.failures[0] + (f"  (+{n - 1} more)" if n > 1 else ""), "",
                     f"{self.case.name}: {n} check(s) failed",
                     *(f"  x {f}" for f in exc.failures), ""]
            if self._resp is not None or self._prepared is not None:
                parts.append(describe_exchange(self._resp, self._prepared))
            parts += ["", f"Re-run this suite with the same data: {rerun}"]
            return "\n".join(parts)
        if isinstance(exc, (UnresolvedVariable, UnknownDynamicVariable, ValueError)):
            return f"{self.case.name}: {exc}"
        return super().repr_failure(excinfo, style)

    def reportinfo(self):
        return self.path, None, f"{self.parent.spec.name} :: {self.name}"


def _as_text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


# =============================================================== fixtures

@pytest.fixture
def api_vars(pytestconfig: pytest.Config) -> Dict[str, Any]:
    """Environment + CLI variables (copy), e.g. ``api_vars['baseUrl']``."""
    return dict(pytestconfig.stash[CTX_KEY].global_scope())


@pytest.fixture(scope="session")
def data(pytestconfig: pytest.Config) -> DataGen:
    """Seeded synthetic data generator (``data.person()``, ``data.resolve('$randomEmail')``...)."""
    return pytestconfig.stash[CTX_KEY].datagen


@pytest.fixture
def api(pytestconfig: pytest.Config, api_vars: Dict[str, Any]):
    """HTTP client bound to the environment's ``baseUrl``."""
    ctx: RunContext = pytestconfig.stash[CTX_KEY]
    base = Renderer(api_vars, ctx.datagen).render(api_vars.get("baseUrl")) if api_vars.get("baseUrl") else None
    client = ApiClient(base_url=base, timeout=ctx.timeout, verify=ctx.verify)
    yield client
    client.close()
