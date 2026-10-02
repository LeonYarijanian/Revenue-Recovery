"""QuickBooks OAuth 2.0: connect, callback, refresh and revoke (Section 6)."""

import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.enums import ConnectionStatus, QboEnvironment
from app.models import QboConnection
from app.weeks import as_utc, to_db

logger = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
API_BASE = {
    QboEnvironment.SANDBOX: "https://sandbox-quickbooks.api.intuit.com",
    QboEnvironment.PRODUCTION: "https://quickbooks.api.intuit.com",
}
REFRESH_MARGIN = timedelta(minutes=5)

# Tests replace this with httpx.MockTransport; None means the real network.
TRANSPORT: httpx.BaseTransport | None = None


class QboAuthError(Exception):
    """Intuit refused the authorization code or refresh token."""


class QboTokenServiceError(Exception):
    """Intuit's token endpoint failed for a reason other than a refused grant."""


def http_client() -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, timeout=30.0)


def configured_environment() -> QboEnvironment:
    if get_settings().QBO_ENVIRONMENT.strip().lower() == "production":
        return QboEnvironment.PRODUCTION
    return QboEnvironment.SANDBOX


def authorization_url(state: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.QBO_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": settings.QBO_REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


def _token_request(client: httpx.Client, form: dict) -> dict:
    settings = get_settings()
    response = client.post(
        TOKEN_URL,
        data=form,
        auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
        headers={"Accept": "application/json"},
    )
    if response.status_code in (400, 401):
        try:
            error = str(response.json().get("error", ""))
        except ValueError:
            error = ""
        detail = f"{response.status_code} {error}".strip()
        raise QboAuthError(f"Intuit refused the token request ({detail}).")
    if response.status_code != 200:
        raise QboTokenServiceError(f"Intuit token endpoint returned HTTP {response.status_code}.")
    payload = response.json()
    for name in ("access_token", "refresh_token", "expires_in"):
        if name not in payload:
            raise QboTokenServiceError(f"Intuit token response is missing {name}.")
    return payload


def exchange_code(client: httpx.Client, code: str) -> dict:
    return _token_request(
        client,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": get_settings().QBO_REDIRECT_URI},
    )


def _store_tokens(connection: QboConnection, payload: dict, now: datetime) -> None:
    connection.access_token_enc = encrypt(payload["access_token"])
    connection.refresh_token_enc = encrypt(payload["refresh_token"])
    connection.access_expires_at = to_db(as_utc(now) + timedelta(seconds=int(payload["expires_in"])))
    connection.last_refreshed_at = to_db(now)


def connection_for(db: Session, company_id: int) -> QboConnection | None:
    return db.scalar(select(QboConnection).where(QboConnection.company_id == company_id))


def save_connection(db: Session, company_id: int, realm_id: str, payload: dict, now: datetime) -> QboConnection:
    connection = connection_for(db, company_id)
    if connection is None:
        connection = QboConnection(company_id=company_id, realm_id=realm_id, environment=configured_environment())
        db.add(connection)
    connection.realm_id = realm_id
    connection.environment = configured_environment()
    connection.status = ConnectionStatus.CONNECTED
    connection.connected_at = to_db(now)
    _store_tokens(connection, payload, now)
    db.commit()
    return connection


def refresh(db: Session, connection: QboConnection, client: httpx.Client, now: datetime) -> None:
    """Get new tokens; both are saved because the refresh token can change.

    Commits right away so a later sync rollback never loses the newest refresh token.
    """
    try:
        if not connection.refresh_token_enc:
            raise QboAuthError("No refresh token is stored.")
        payload = _token_request(
            client, {"grant_type": "refresh_token", "refresh_token": decrypt(connection.refresh_token_enc)}
        )
    except QboAuthError:
        connection.status = ConnectionStatus.NEEDS_RECONNECT
        db.commit()
        logger.warning("QuickBooks refresh refused for company %s; reconnect needed.", connection.company_id)
        raise
    _store_tokens(connection, payload, now)
    db.commit()


def ensure_access_token(db: Session, connection: QboConnection, client: httpx.Client, now: datetime) -> str:
    """Refresh first if the access token expires within 5 minutes."""
    expires = connection.access_expires_at
    if expires is None or as_utc(expires) - as_utc(now) <= REFRESH_MARGIN:
        refresh(db, connection, client, now)
    return decrypt(connection.access_token_enc)


def disconnect(db: Session, connection: QboConnection, client: httpx.Client) -> None:
    """Revoke the refresh token, delete both tokens and mark the connection DISCONNECTED."""
    if connection.refresh_token_enc:
        settings = get_settings()
        try:
            client.post(
                REVOKE_URL,
                json={"token": decrypt(connection.refresh_token_enc)},
                auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            logger.warning("QuickBooks revoke call failed for company %s; tokens deleted anyway.", connection.company_id)
    connection.access_token_enc = None
    connection.refresh_token_enc = None
    connection.access_expires_at = None
    connection.status = ConnectionStatus.DISCONNECTED
    db.commit()
