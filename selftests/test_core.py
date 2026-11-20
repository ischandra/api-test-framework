import pytest

from apitest import jsonpath
from apitest.assertions import _MISSING, match
from apitest.datagen import DataGen, UnknownDynamicVariable
from apitest.templating import Renderer, UnresolvedVariable, placeholder_names

DOC = {"id": 7, "name": "A", "items": [{"id": 1, "tags": ["x"]}, {"id": 2}], "odd key": True}


@pytest.mark.parametrize("path,expected", [
    ("$", DOC), ("$.id", 7), ("id", 7), ("$.items[0].id", 1), ("$.items[-1].id", 2),
    ("$.items[*].id", [1, 2]), ("$.items.length", 2), ("$.name.length", 1),
    ("$['odd key']", True), ("$.items[0].tags[0]", "x"),
])
def test_jsonpath_extract(path, expected):
    assert jsonpath.extract(DOC, path) == expected


@pytest.mark.parametrize("path", ["$.nope", "$.items[5]", "$.id.x", "$.items[0].missing"])
def test_jsonpath_missing(path):
    with pytest.raises(jsonpath.PathNotFound):
        jsonpath.extract(DOC, path)


def test_jsonpath_invalid():
    with pytest.raises(jsonpath.InvalidPath):
        jsonpath.parse("$.a[")


@pytest.mark.parametrize("actual,expected,ok", [
    (5, 5, True), (5, 6, False), (True, 1, False), (1, True, False), ({"a": [1]}, {"a": [1]}, True),
    (5, {"gt": 4, "lt": 6}, True), (5, {"gte": 6}, False),
    ("hello", {"contains": "ell"}, True), ([1, 2], {"contains": 2}, True), ({"k": 1}, {"contains": "k"}, True),
    ([1, 2], {"not_contains": 3}, True), ("abc123", {"regex": r"^\w+\d{3}$"}, True),
    ([1, 2, 3], {"len": 3}, True), ([1, 2, 3], {"len": {"gt": 5}}, False),
    (1, {"type": "integer"}, True), (True, {"type": "integer"}, False), (1.5, {"type": "number"}, True),
    (None, {"type": ["string", "null"]}, True), ("B", {"one_of": ["A", "B"]}, True),
    ("", {"not_empty": True}, False), (_MISSING, {"exists": False}, True), (_MISSING, {"exists": True}, False),
    (_MISSING, 5, False), (3, {"ne": 4}, True),
])
def test_matchers(actual, expected, ok):
    assert (match(actual, expected) is None) is ok


def test_datagen_is_reproducible_by_seed():
    names = ["$randomFullName", "$randomEmail", "$guid", "$int(1,100)", "$pattern(AB-###-??)", "$oneOf(x,y,z)",
             "$randomPrice", "$string(12)", "$date(-10,10)"]
    a, b, c = DataGen(42), DataGen(42), DataGen(43)
    va, vb, vc = ([g.resolve(n) for n in names] for g in (a, b, c))
    assert va == vb
    assert va != vc


def test_datagen_unique_values_differ_per_run_but_not_within():
    a, b = DataGen(1), DataGen(1)
    emails = [a.resolve("$uniqueEmail") for _ in range(200)]
    assert len(set(emails)) == 200
    assert a.resolve("$uniqueEmail") != b.resolve("$uniqueEmail")


def test_datagen_safe_values():
    g = DataGen(3)
    for _ in range(100):
        assert g.resolve("$randomEmail").split("@")[1] in {"example.com", "example.org", "example.net"}
        assert "-555-" in g.resolve("$randomPhoneNumber")
        assert g.resolve("$randomIP").startswith(("192.0.2.", "198.51.100.", "203.0.113."))
    assert len(g.resolve("$string(256)")) == 256


def test_datagen_unknown():
    with pytest.raises(UnknownDynamicVariable, match="Known:"):
        DataGen(1).resolve("$nope")


def test_render_preserves_types_and_interpolates():
    r = Renderer({"id": 5, "flag": True, "obj": {"a": "{{id}}"}, "host": "h", "url": "http://{{host}}/x"}, DataGen(1))
    assert r.render("{{id}}") == 5
    assert r.render("{{ flag }}") is True
    assert r.render("{{obj}}") == {"a": 5}
    assert r.render("/users/{{id}}?f={{flag}}") == "/users/5?f=true"
    assert r.render("{{url}}") == "http://h/x"
    assert isinstance(r.render("{{$int(1,3)}}"), int)
    assert r.render({"k{{id}}": ["{{id}}"]}) == {"k5": [5]}


def test_render_unresolved_has_hint():
    r = Renderer({}, DataGen(1), hints={"token": "Login"})
    with pytest.raises(UnresolvedVariable, match="saved by step 'Login'"):
        r.render("Bearer {{token}}")


def test_placeholder_names():
    assert placeholder_names({"a": "{{x}}/{{ $guid }}", "b": ["{{y}}"]}) == {"x", "$guid", "y"}
