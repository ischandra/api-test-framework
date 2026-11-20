# API Test Framework

API test automation on **Python + pytest** with three ways to describe tests:

| Input | Use it for | Example |
|---|---|---|
| **API URLs directly** | Smoke checks with no setup | `python -m apitest run https://api.example.com/health` |
| **YAML suites** (`*.api.yaml`) | Full API flows written as data: chaining, data-driven, negative tests | [`suites/users.api.yaml`](suites/users.api.yaml) |
| **Postman collections** | Running existing collections as-is, in CI | [`collections/`](collections/) |

For logic that doesn't fit in YAML, you can also write plain pytest tests using the `api` and `data` fixtures. See [`tests/`](tests/).

All of these run through pytest, so you get JUnit XML for Jenkins, `-k` / `-m` filtering, and parallel runs across files.

**Highlights**
- **Synthetic test data:** `{{$randomEmail}}`, `{{$int(1,100)}}`, `{{$pattern(ORD-###)}}` and more. Data is seeded, so `--seed` reproduces a run exactly.
- **Assertions:** status, response time, headers, JSON paths with matchers, JSON Schema, and body text.
- **Chaining:** `save: {token: $.token}` passes values to later steps. Filtering with `-k`/`-m` automatically keeps the steps a selected test depends on.
- **Failure reports:** each failure lists every failed check, the request and response, and a `curl` command to reproduce it. Tokens, passwords and API keys are masked.
- **Postman support:** imports requests, folders, variables, environments, auth (inherited), bodies and path variables. Common `pm.*` test scripts become assertions, and untranslated lines are reported rather than silently dropped.
- **Built-in demo API:** you can try everything offline.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate      # Python 3.9+
pip install -r requirements.txt

python -m apitest demo-server &                    # demo API on http://localhost:8000
pytest                                             # run all suites, collections and tests/
```

Useful variations:

```bash
pytest -m smoke                                    # by tag
pytest suites/users.api.yaml -k "Create"           # one suite, by name
pytest --env dev                                   # another environment (environments/dev.yaml)
pytest --base-url https://qa.example.com           # override the base URL
pytest --seed 1234                                 # reproduce the data of a previous run
pytest --var token=abc --var tenant=acme           # set or override variables
pytest -o log_cli=true --log-cli-level=INFO        # print every request/response line
pytest --junitxml=reports/junit.xml                # report for Jenkins
```

## 1. Testing API URLs directly

```bash
python -m apitest run https://api.example.com/health https://api.example.com/v1/status
python -m apitest run "HEAD https://api.example.com/health" --expect-status 200 --max-time-ms 500
python -m apitest run https://api.example.com/v1/me -H "Authorization: Bearer $TOKEN"
```

To keep a list of URLs in the repo, use the `smoke` section of a suite ([`suites/smoke.api.yaml`](suites/smoke.api.yaml)):

```yaml
name: Smoke
defaults:
  expect: {max_time_ms: 2000}
smoke:
  - /health                       # GET, expects 2xx
  - https://other-host/status     # absolute URLs work too
  - HEAD /health
  - url: /users/999999
    status: 404
```

## 2. YAML suites

Steps run top to bottom. `{{name}}` inserts a variable, and `{{$name}}` inserts generated data.

```yaml
name: Users API
tags: [users]
variables:
  email: "{{$uniqueEmail}}"         # rendered once per run, so it's the same in every step
defaults:
  headers: {Accept: application/json}
  expect: {max_time_ms: 3000}

tests:
  - name: Login
    post: /auth/login
    json: {username: "{{username}}", password: "{{password}}"}
    expect:
      status: 200
      json:
        $.token: {type: string, not_empty: true}
    save:
      token: $.token                # available as {{token}} from now on

  - name: Create user
    post: /users
    auth: {bearer: "{{token}}"}
    json: {name: "{{$randomFullName}}", email: "{{email}}", age: "{{$int(18, 90)}}"}
    expect:
      status: 201
      headers: {Location: {regex: "^/users/\\d+$"}}
      json:
        $.id: {type: integer}
        $.email: "{{email}}"
      schema: ../schemas/user.json
    save: {userId: $.id}

  - name: Validation errors          # data-driven: one test per case
    post: /users
    auth: {bearer: "{{token}}"}
    json: "{{payload}}"
    cases:
      - {id: missing-name, payload: {email: "{{$randomEmail}}"}}
      - {id: long-name,    payload: {name: "{{$string(101)}}", email: "{{$randomEmail}}"}}
    expect: {status: 400}
```

### Test step keys

| Key | Meaning |
|---|---|
| `get` / `post` / `put` / `patch` / `delete` / `head` / `options: <url>` | Method and URL. Relative URLs are joined to `baseUrl` |
| `headers`, `params` | Request headers and query parameters |
| `json` / `body` / `form` / `multipart` | JSON body, raw text body, URL-encoded form, or multipart text fields |
| `auth` | `{bearer: token}`, `{basic: {username, password}}`, `{api_key: {name, value, in: header\|query}}`, or `none` |
| `expect` | See [assertions](#assertions) below |
| `save` | `{var: $.json.path}`, `{var: "header:Location"}`, `{var: status}`, `{var: body}` |
| `cases` | List of variable sets. Creates one test per entry; `id` names it |
| `data_file` | Same as `cases`, read from a CSV, JSON or YAML file |
| `repeat` | Run the step N times, with fresh generated data each time |
| `tags`, `skip`, `timeout`, `variables`, `notes` | Tags become pytest markers (`-m`); `skip: "reason"` skips the step |

Suite-level keys are `name`, `base_url` (default `{{baseUrl}}`), `variables`, `defaults` (`headers`, `params`, `auth`, `timeout`, `expect`), `tags`, `tests` and `smoke`.

Unknown keys are reported as errors, so typos like `gett:` fail loudly instead of being ignored.

### Assertions

```yaml
expect:
  status: 201                  # or [200, 204], "2xx", {gte: 200, lt: 300}. Default: 2xx
  max_time_ms: 1500
  headers:
    Content-Type: {contains: json}
  json:
    $.id: {type: integer}
    $.email: "{{email}}"            # a plain value means "equals"
    $.items.length: {gt: 0}
    $.items[*].status: {contains: ACTIVE}
    $.items[0].tags: {len: {lte: 5}}
    $.deletedAt: {exists: false}
  body_contains: ["created"]
  schema: ../schemas/user.json      # JSON Schema: a file path (relative to the suite) or inline
```

Matchers can be combined, e.g. `{type: integer, gt: 0}`. The available matchers are: `eq ne gt gte lt lte contains not_contains regex len type exists one_of not_empty`.

JSON paths support `$.a.b`, `$.list[0]`, `$.list[-1]`, `$.list[*].id`, `$['key with spaces']` and `.length`.

## 3. Postman collections

Put collection exports (`*.postman_collection.json`) in `collections/` and they run with `pytest`. You can also run them from anywhere:

```bash
python -m apitest run -c path/to/My.postman_collection.json --postman-env path/to/Dev.postman_environment.json
python -m apitest run -c "https://api.getpostman.com/collections/<uid>?apikey=$POSTMAN_API_KEY"
pytest collections/ --postman-env collections/demo-local.postman_environment.json
```

**How Postman features map:**
- **Converted directly:** folders (test names become `Folder / Request`), collection variables, environments, Postman dynamic variables (`{{$guid}}`, `{{$randomEmail}}`, …), auth inherited from collection or folder (`bearer`, `basic`, `apikey`, OAuth2 access token), raw/JSON, urlencoded, form-data and GraphQL bodies, and `:path` variables.
- **Test scripts:** JavaScript can't run here, so common assertions are translated. That covers `pm.response.to.have.status(...)`, `pm.response.to.be.ok/notFound/...`, `pm.expect(jsonData.x).to.eql/include/be.a/be.above/have.property/match/...`, header and response-time checks, `pm.environment.set("token", jsonData.token)` and legacy `tests[...]` syntax. Collection- and folder-level scripts apply to every request beneath them.
- **Anything else** shows up as a warning in the pytest summary, so you know what wasn't covered.
- **Default status check:** a request with no status assertion must return a 2xx.

To take over a collection and keep extending it in YAML:

```bash
python -m apitest import-postman collections/demo-users.postman_collection.json -o suites/demo-users.api.yaml
```

## 4. Code-based tests

```python
from apitest import check

def test_create_user(api, data, api_vars):
    person = data.person()                                  # seeded name, email and phone
    resp = api.post("/users", json={"name": person["name"], "email": person["email"]},
                    auth={"bearer": api_vars["token"]})
    check(resp, status=201, json={"$.id": {"type": "integer"}})
```

The fixtures are:
- `api`: a requests-based client bound to `baseUrl`
- `data`: the seeded data generator
- `api_vars`: environment and `--var` values

`check()` uses the same assertion engine as the YAML suites, and produces the same masked request/response report on failure.

## Environments and secrets

`environments/<name>.yaml` holds per-environment variables. Select one with `--env <name>` or the `API_ENV` variable. If neither is set, `local` is used.

```yaml
baseUrl: ${API_BASE_URL:-https://dev-api.example.com}
username: ${API_USERNAME}            # read from OS environment variables, e.g. Jenkins credentials
password: ${API_PASSWORD}
```

Don't commit secrets. Unset variables are listed as a warning in the test report header.

**Variable precedence** (highest wins):
1. `--var`
2. per-case data (`cases` / `data_file`)
3. values saved by earlier steps
4. Postman environment
5. environment file
6. suite or collection `variables`

## Synthetic data

Run `python -m apitest dynamic-vars` for the full list. The main groups are:

| Group | Examples |
|---|---|
| Postman-compatible | `$guid` `$timestamp` `$isoTimestamp` `$randomInt` `$randomFirstName` `$randomFullName` `$randomEmail` `$randomPhoneNumber` `$randomCity` `$randomCompanyName` `$randomPrice` `$randomBoolean` `$randomUrl` `$randomIP` … |
| Parametrised | `$int(1,100)` `$decimal(1,500,2)` `$oneOf(A,B,C)` `$pattern(ORD-####-??)` (`#` digit, `?` letter, `*` alphanumeric) `$string(256)` `$string(8,hex)` `$date(-30,0)` `$email(corp.example.com)` `$now` `$today` |
| Unique per run | `$uniqueEmail` `$uniqueUserName` `$unique` `$runId`: for fields with uniqueness constraints, so re-runs never collide with old data |
| Negative testing | `$edgeString` `$sqlInjection` `$xss` `$unicode` `$empty` `$whitespace` |

**How seeding works:** everything except the "unique per run" values is reproducible from `--seed`, and the seed is printed in the report header. All contact data is fictional: `example.*` domains, 555 phone numbers, documentation-range IPs, and public test card numbers.

A string that is exactly one placeholder (`"{{$int(1,5)}}"`, `"{{payload}}"`) keeps its type, so numbers, booleans and objects stay typed in JSON bodies.

## CI

- **Jenkins:** [`Jenkinsfile`](Jenkinsfile) sets up a virtualenv, runs the self-tests, starts the demo API when `API_ENV=local`, runs the API tests with `--seed $BUILD_NUMBER`, and publishes JUnit results. Its parameters are `API_ENV`, `TEST_SELECTION` (e.g. `-m smoke`) and `SEED`.
- **GitHub Actions:** [`.github/workflows/api-tests.yml`](.github/workflows/api-tests.yml) does the same on every push.

## Project layout

```
apitest/            framework: pytest plugin, loaders (YAML, Postman), assertions, client, data generator, demo API, CLI
suites/             *.api.yaml suites (direct API URLs and flows)
collections/        Postman collections and environments
tests/              code-based pytest API tests
environments/       per-environment variables (local, dev, ...)
schemas/  data/     JSON Schemas and data files used by suites
selftests/          tests for the framework itself (pytest selftests)
```

## Notes

- **Step order matters within a file.** Steps chain through `save`, and pytest runs them in file order. If you use `pytest-xdist` to run in parallel, use `--dist loadfile` so each file stays on one worker.
- **Saved values don't cross files.** Each suite file keeps its own saved values. To share a token across suites, pass it with `--var token=...` or log in within each suite.
