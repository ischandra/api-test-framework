"""Tests for the framework itself (not API tests). Run: pytest selftests"""
import pytest

from apitest.demo_server import start_in_thread

pytest_plugins = ["pytester"]


@pytest.fixture(scope="session")
def demo_url():
    server, url = start_in_thread()
    yield url
    server.shutdown()
