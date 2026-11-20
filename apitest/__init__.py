"""apitest - API test automation on pytest.

Describe APIs directly as URLs in ``*.api.yaml`` suites, run Postman collections
as-is, or write plain pytest tests with the ``api`` / ``data`` fixtures.
"""
__all__ = ["ApiClient", "ApiCheckFailed", "DataGen", "check"]
__version__ = "0.1.0"

_EXPORTS = {"ApiClient": "client", "ApiCheckFailed": "assertions", "DataGen": "datagen", "check": "assertions"}


def __getattr__(name):  # lazy, so `python -m apitest` doesn't import Faker before pytest starts
    if name in _EXPORTS:
        import importlib
        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(f"module 'apitest' has no attribute '{name}'")
