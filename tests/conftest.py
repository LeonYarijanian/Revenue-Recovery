import os
import re

import httpx
import pytest
from cryptography.fernet import Fernet

TEST_SECRET = "0123456789abcdef" * 4
TEST_FERNET_KEY = Fernet.generate_key().decode()
TEST_ENV = {
    "APP_SECRET_KEY": TEST_SECRET,
    "TOKEN_ENCRYPTION_KEY": TEST_FERNET_KEY,
    "DATABASE_URL": "sqlite://",
    "SCHEDULER_ENABLED": "false",
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "LOG_LEVEL": "WARNING",
    "QBO_CLIENT_ID": "test-client-id",
    "QBO_CLIENT_SECRET": "test-client-secret",
    "QBO_REDIRECT_URI": "http://localhost:8000/qbo/callback",
    "QBO_ENVIRONMENT": "sandbox",
    "ALLOW_LIVE_EMAIL": "false",
    "POSTMARK_SERVER_TOKEN": "",
    "REPORT_FROM_EMAIL": "",
    "REPORT_FROM_NAME": "",
    "OPERATOR_EMAIL": "",
    "SENDER_DOMAIN_VERIFIED": "false",
}
# The app module builds an app at import time, so valid settings must exist first.
os.environ.update(TEST_ENV)

from app.config import get_settings  # noqa: E402
from app.sources import qbo, qbo_oauth  # noqa: E402
from app import mailer  # noqa: E402
from tests.fixtures import build_base_fixture  # noqa: E402


class NetworkBlocked(Exception):
    pass


def _block(request: httpx.Request) -> httpx.Response:
    raise NetworkBlocked(f"Real network call attempted in a test: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def test_env(tmp_path, monkeypatch):
    """Fresh settings, a fresh SQLite file in tmp_path, ./outbox in tmp_path, and no real network."""
    monkeypatch.chdir(tmp_path)
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    monkeypatch.setattr(qbo_oauth, "TRANSPORT", httpx.MockTransport(_block))
    monkeypatch.setattr(mailer, "TRANSPORT", httpx.MockTransport(_block))
    monkeypatch.setattr(qbo, "SLEEP", lambda seconds: None)
    yield
    get_settings.cache_clear()


@pytest.fixture
def base():
    return build_base_fixture()


@pytest.fixture
def app_factory():
    from app.main import create_app

    return create_app


@pytest.fixture
def client(base, app_factory):
    from fastapi.testclient import TestClient

    with TestClient(app_factory(), follow_redirects=False) as test_client:
        yield test_client


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(client, path: str = "/login") -> str:
    response = client.get(path)
    match = CSRF_RE.search(response.text)
    assert match, f"No CSRF token on {path} (status {response.status_code})"
    return match.group(1)


def login(client, email: str, password: str = "test-password-123"):
    token = csrf_from(client, "/login")
    return client.post("/login", data={"email": email, "password": password, "csrf_token": token})


def post_form(client, path: str, data: dict, csrf_page: str = "/account"):
    token = csrf_from(client, csrf_page)
    return client.post(path, data={**data, "csrf_token": token})
