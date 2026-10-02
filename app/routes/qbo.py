"""Connect, callback and disconnect for QuickBooks (Section 6)."""

import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import can_access_company, current_user
from app.db import get_db
from app.enums import Role
from app.models import Company, User
from app.routes.common import set_flash
from app.sources import qbo_oauth
from app.sources.qbo_oauth import QboAuthError, QboTokenServiceError, connection_for
from app.sync import run_sync
from app.weeks import utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

__all__ = ["router", "connection_for"]


def _company_for(db: Session, user: User, company_id: int | None) -> Company:
    if company_id is None:
        company_id = user.company_id
    company = db.get(Company, company_id) if company_id else None
    if company is None or not can_access_company(user, company.id):
        raise HTTPException(status_code=404)
    return company


def _after_url(user: User, company: Company) -> str:
    return f"/companies/{company.id}?tab=connection" if user.role == Role.OPERATOR else "/account"


@router.get("/qbo/connect")
def connect(
    request: Request,
    company_id: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    company = _company_for(db, user, company_id if company_id is not None else request.session.get("company_id"))
    state = secrets.token_urlsafe(24)
    request.session["qbo_state"] = state
    request.session["qbo_company_id"] = company.id
    return RedirectResponse(qbo_oauth.authorization_url(state), status_code=302)


@router.get("/qbo/callback")
def callback(
    request: Request,
    code: str = "",
    realmId: str = "",  # noqa: N803 (Intuit's parameter name)
    state: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    expected = request.session.get("qbo_state")
    if not expected or not state or not secrets.compare_digest(expected, state):
        raise HTTPException(status_code=400, detail="QuickBooks state does not match. Start the connection again.")
    company = _company_for(db, user, request.session.get("qbo_company_id"))
    request.session.pop("qbo_state", None)
    request.session.pop("qbo_company_id", None)
    if not code or not realmId:
        raise HTTPException(status_code=400, detail="QuickBooks did not return a code and realm ID.")
    now = utcnow()
    with qbo_oauth.http_client() as client:
        try:
            payload = qbo_oauth.exchange_code(client, code)
        except (QboAuthError, QboTokenServiceError) as exc:
            logger.warning("QuickBooks code exchange failed for company %s.", company.id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    qbo_oauth.save_connection(db, company.id, realmId[:32], payload, now)
    run = run_sync(db, company.id, now)
    status = run.status if run else "skipped"
    set_flash(request, f"QuickBooks connected. First sync: {status}.")
    return RedirectResponse(_after_url(user, company), status_code=302)


@router.post("/qbo/disconnect")
def disconnect(
    request: Request,
    company_id: int | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    company = _company_for(db, user, company_id)
    connection = connection_for(db, company.id)
    if connection is not None:
        with qbo_oauth.http_client() as client:
            qbo_oauth.disconnect(db, connection, client)
    set_flash(request, "QuickBooks disconnected.")
    return RedirectResponse(_after_url(user, company), status_code=303)
