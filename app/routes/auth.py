"""Login, logout, account (change password) and the operator's company switcher."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    LOCKOUT_MESSAGE,
    WRONG_DETAILS_MESSAGE,
    authenticate,
    current_user,
    hash_password,
    locked_until,
    password_error,
    record_attempt,
    require_operator,
    rotate_csrf,
    verify_password,
)
from app.db import get_db
from app.models import Company, User
from app.routes.common import page_context, render
from app.weeks import utcnow

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", {"error": None, "email": ""})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    now = utcnow()
    email = email.strip().lower()
    if locked_until(db, email, now):
        return render(request, "login.html", {"error": LOCKOUT_MESSAGE, "email": email})
    user = authenticate(db, email, password)
    record_attempt(db, email, now, succeeded=user is not None)
    if user is None:
        return render(request, "login.html", {"error": WRONG_DETAILS_MESSAGE, "email": email})
    request.session.clear()
    request.session["user_id"] = user.id
    rotate_csrf(request)
    return RedirectResponse("/", status_code=302)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/account")
def account_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx = page_context(request, db, user)
    ctx.update(_account_extras(db, ctx))
    return render(request, "account.html", ctx)


@router.post("/account")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    error = None
    if not verify_password(user.password_hash, current_password):
        error = "Current password is incorrect."
    elif new_password != confirm_password:
        error = "The new passwords do not match."
    else:
        error = password_error(new_password)
    if error is None and verify_password(user.password_hash, new_password):
        error = "Choose a password different from the current one."
    ctx = page_context(request, db, user)
    ctx.update(_account_extras(db, ctx))
    if error:
        ctx["error"] = error
        return render(request, "account.html", ctx, status_code=400)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    ctx["message"] = "Password changed."
    return render(request, "account.html", ctx)


def _account_extras(db: Session, ctx: dict) -> dict:
    from app.routes.qbo import connection_for

    company = ctx.get("company")
    return {"connection": connection_for(db, company.id) if company else None}


@router.post("/switch-company")
def switch_company(
    request: Request,
    company_id: int = Form(...),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    if db.get(Company, company_id) is None:
        raise HTTPException(status_code=404)
    request.session["company_id"] = company_id
    return RedirectResponse("/", status_code=303)
