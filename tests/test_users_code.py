"""Code-based API tests: use plain pytest when a scenario needs real logic.

Fixtures (from apitest.plugin):
  api       HTTP client bound to the environment's baseUrl
  data      seeded synthetic data (same --seed => same data)
  api_vars  environment / --var variables
"""
import pytest

from apitest import check


@pytest.fixture
def token(api, api_vars):
    resp = api.post("/auth/login", json={"username": api_vars["username"], "password": api_vars["password"]})
    return check(resp, status=200).json()["token"]


@pytest.mark.users
def test_create_then_fetch_user(api, data, token):
    person = data.person()
    auth = {"bearer": token}

    created = check(api.post("/users", json={"name": person["name"], "email": person["email"]}, auth=auth),
                    status=201, json={"$.id": {"type": "integer"}, "$.email": person["email"]}).json()

    check(api.get(f"/users/{created['id']}"), status=200, json={"$.name": person["name"]})


@pytest.mark.negative
@pytest.mark.parametrize("bad_email", ["", "plain", "a@b", "two@@example.com", "spaces in@example.com"])
def test_rejects_invalid_email(api, data, token, bad_email):
    resp = api.post("/users", json={"name": data.resolve("$randomFullName"), "email": bad_email},
                    auth={"bearer": token})
    check(resp, status=400, json={"$.details.email": {"exists": True}})
