"""Runs the bundled example suites/collection against the demo API through pytest itself."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(pytester):
    for d in ("suites", "collections", "environments", "schemas", "data", "tests"):
        shutil.copytree(ROOT / d, pytester.path / d)
    shutil.copy(ROOT / "pytest.ini", pytester.path / "pytest.ini")
    return pytester


def _outcomes(result):
    o = result.parseoutcomes()
    return o.get("passed", 0), o.get("failed", 0), o.get("errors", 0) + o.get("error", 0)


def test_examples_pass(project, demo_url):
    result = project.runpytest("-p", "apitest.plugin", "--base-url", demo_url, "--seed", "1")
    passed, failed, errors = _outcomes(result)
    assert (failed, errors) == (0, 0), result.stdout.str()
    assert passed >= 30
    # same seed again: run-unique values must prevent conflicts with data from run 1
    result = project.runpytest("-p", "apitest.plugin", "--base-url", demo_url, "--seed", "1")
    assert _outcomes(result)[1:] == (0, 0), result.stdout.str()


def test_tag_selection_pulls_in_prerequisites(project, demo_url):
    result = project.runpytest("-p", "apitest.plugin", "--base-url", demo_url, "-m", "negative", "suites")
    result.stdout.fnmatch_lines(["*added 1 prerequisite step(s)*Login*"])
    assert _outcomes(result)[1:] == (0, 0)


def test_failure_report(pytester, demo_url):
    pytester.makefile(".api.yaml", bad="""
name: Bad
tests:
  - name: Login
    post: /auth/login
    json: {username: demo, password: demo-pass}
    save: {token: $.token}
  - name: Wrong
    post: /users
    auth: {bearer: "{{token}}"}
    json: {name: x, email: "{{$uniqueEmail}}", password: s3cret}
    expect:
      status: 200
      json: {$.id: {type: string}}
    save: {userId: $.id}
  - name: Dependent
    get: /users/{{userId}}
""")
    result = pytester.runpytest("-p", "apitest.plugin", "--base-url", demo_url)
    out = result.stdout.str()
    assert _outcomes(result) == (1, 2, 0)
    assert "x status: expected 200, got 201" in out
    assert "x $.id: expected type string" in out
    assert "Authorization: ***" in out and '"password": "***"' in out
    assert "s3cret" not in out
    assert "curl -X POST" in out
    assert "saved by step 'Wrong'" in out


def test_invalid_suite_is_a_collection_error(pytester):
    pytester.makefile(".api.yaml", broken="tests:\n  - name: x\n    gett: /typo\n")
    result = pytester.runpytest("-p", "apitest.plugin")
    assert "unknown key(s) ['gett']" in result.stdout.str()
    assert result.ret != 0


def test_cli_direct_urls(tmp_path, demo_url):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    ok = subprocess.run([sys.executable, "-m", "apitest", "run", f"{demo_url}/health", f"HEAD {demo_url}/users",
                         "--junit", ""], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "2 passed" in ok.stdout
    bad = subprocess.run([sys.executable, "-m", "apitest", "run", f"{demo_url}/missing", "--junit", ""],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert bad.returncode == 1 and "expected 2xx, got 404" in bad.stdout


def test_cli_import_postman_round_trip(tmp_path, demo_url):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = tmp_path / "imported.api.yaml"
    r = subprocess.run([sys.executable, "-m", "apitest", "import-postman",
                        str(ROOT / "collections" / "demo-users.postman_collection.json"), "-o", str(out)],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (tmp_path / "environments").mkdir()
    shutil.copy(ROOT / "environments" / "local.yaml", tmp_path / "environments" / "local.yaml")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "apitest.plugin", str(out), "--base-url", demo_url,
                        "-q", "-p", "no:cacheprovider"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    assert "7 passed" in r.stdout
