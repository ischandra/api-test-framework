import json

from apitest.loaders import postman
from apitest.loaders.postman import ScriptTranslator


def tr(*lines):
    t = ScriptTranslator()
    t.translate(list(lines))
    return t


def test_status_patterns():
    assert tr("pm.response.to.have.status(201);").expect["status"] == 201
    assert tr('pm.response.to.have.status("Created");').expect["status"] == 201
    assert tr("pm.expect(pm.response.code).to.eql(204);").expect["status"] == 204
    assert tr("pm.expect(pm.response.code).to.be.oneOf([200, 201]);").expect["status"] == [200, 201]
    assert tr("pm.response.to.be.notFound;").expect["status"] == 404
    assert tr("pm.response.to.be.success;").expect["status"] == "2xx"
    assert tr('tests["ok"] = responseCode.code === 200;').expect["status"] == 200


def test_json_assertions_and_aliases():
    t = tr("const res = pm.response.json();",
           "pm.expect(res.data.items[0].id).to.eql(5);",
           "pm.expect(res.name).to.be.a('string');",
           "pm.expect(res.items.length).to.be.above(0);",
           "pm.expect(res).to.have.property('token');",
           "pm.expect(res.tags).to.include('x');",
           "pm.expect(res.code).to.match(/^AB\\d+$/i);",
           "pm.expect(res.deleted).to.be.false;",
           "pm.expect(res.owner).to.eql(pm.environment.get(\"ownerId\"));",
           "pm.expect(pm.response.json().total).to.be.at.least(1);")
    j = t.expect["json"]
    assert j["$.data.items[0].id"] == {"eq": 5}
    assert j["$.name"] == {"type": "string"}
    assert j["$.items.length"] == {"gt": 0}
    assert j["$.token"] == {"exists": True}
    assert j["$.tags"] == {"contains": "x"}
    assert j["$.code"] == {"regex": "(?i)^AB\\d+$"}
    assert j["$.deleted"] == {"eq": False}
    assert j["$.owner"] == {"eq": "{{ownerId}}"}
    assert j["$.total"] == {"gte": 1}
    assert t.untranslated == []


def test_headers_time_body_and_saves():
    t = tr("pm.response.to.have.header('Content-Type');",
           "pm.expect(pm.response.headers.get('X-Rate')).to.eql('10');",
           "pm.expect(pm.response.responseTime).to.be.below(500);",
           "pm.expect(pm.response.text()).to.include('ok');",
           "var jsonData = pm.response.json();",
           "pm.environment.set('token', jsonData.access_token);",
           "pm.collectionVariables.set(\"loc\", pm.response.headers.get(\"Location\"));",
           "postman.setEnvironmentVariable('id', jsonData.user.id);")
    e = t.expect
    assert e["headers"] == {"Content-Type": {"exists": True}, "X-Rate": {"eq": "10"}}
    assert e["max_time_ms"] == 500
    assert e["body_contains"] == ["ok"]
    assert t.save == {"token": "$.access_token", "loc": "header:Location", "id": "$.user.id"}


def test_untranslated_lines_are_reported():
    t = tr("pm.test('custom', function () {",
           "    const ok = someHelper(pm.response);",
           "    pm.expect(ok).to.be.true;",
           "});")
    assert len(t.untranslated) == 2


def test_collection_conversion(tmp_path):
    col = {
        "info": {"name": "C", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "baseUrl", "value": "http://x"}],
        "auth": {"type": "apikey", "apikey": [{"key": "key", "value": "X-Key"}, {"key": "value", "value": "{{k}}"}]},
        "item": [{"name": "F", "auth": {"type": "basic", "basic": [{"key": "username", "value": "u"},
                                                                  {"key": "password", "value": "p"}]},
                  "item": [{"name": "Get", "request": {"method": "GET", "url": {
                      "raw": "{{baseUrl}}/a/:id?x=1", "variable": [{"key": "id", "value": "{{id}}"}]}}}]},
                 {"name": "Form", "request": {"method": "POST", "url": "{{baseUrl}}/f",
                                              "header": [{"key": "H", "value": "1"}, {"key": "Off", "value": "0", "disabled": True}],
                                              "body": {"mode": "urlencoded", "urlencoded": [{"key": "a", "value": "1"}]}}},
                 {"name": "Raw", "request": {"method": "POST", "url": "{{baseUrl}}/r", "auth": {"type": "noauth"},
                                             "body": {"mode": "raw", "raw": "{\"n\": {{num}}}",
                                                      "options": {"raw": {"language": "json"}}}}}],
    }
    p = tmp_path / "c.postman_collection.json"
    p.write_text(json.dumps(col))
    assert postman.is_collection(p)
    s = postman.load(p)
    get, form, raw = s.cases
    assert get.name == "F / Get" and get.url == "{{baseUrl}}/a/{{id}}?x=1"
    assert get.auth == {"basic": {"username": "u", "password": "p"}}
    assert form.form == {"a": "1"} and form.headers == {"H": "1"}
    assert form.auth == {"api_key": {"name": "X-Key", "value": "{{k}}", "in": "header"}}
    assert raw.auth is None and raw.body == '{"n": {{num}}}' and raw.headers["Content-Type"] == "application/json"
    assert s.variables == {"baseUrl": "http://x"}
