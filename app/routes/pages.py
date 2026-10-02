"""Home, past reports, invoices and add-backs (Section 11)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, scoped_or_404
from app.db import get_db
from app.enums import AddbackCategory
from app.kpis import aging_bucket, compute_kpis, display
from app.models import Addback, Invoice, Report, User
from app.money import fmt_dollars, parse_dollars
from app.reminders import set_disputed
from app.routes.common import page_context, pop_flash, render, set_flash
from app.sources.qbo_oauth import connection_for
from app.weeks import local_date, report_date_for, utcnow

router = APIRouter()

CATEGORY_LABELS = {
    AddbackCategory.OWNER_PAY: "Owner's pay",
    AddbackCategory.INTEREST: "Interest",
    AddbackCategory.DEPRECIATION_AMORTIZATION: "Depreciation and amortization",
    AddbackCategory.ONE_TIME: "One-time cost",
    AddbackCategory.OTHER: "Other",
}


def _context_or_redirect(request: Request, db: Session, user: User):
    ctx = page_context(request, db, user)
    if ctx["company"] is None:
        return None, RedirectResponse("/operator", status_code=302)
    return ctx, None


@router.get("/")
def home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx, redirect = _context_or_redirect(request, db, user)
    if redirect:
        return redirect
    company = ctx["company"]
    reports = db.scalars(
        select(Report).where(Report.company_id == company.id).order_by(Report.report_date.desc())
    ).all()
    ctx.update(latest=reports[0] if reports else None, reports=reports, connection=connection_for(db, company.id))
    return render(request, "home.html", ctx)


@router.get("/reports/{report_id}")
def past_report(report_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx = page_context(request, db, user)
    report = scoped_or_404(db.get(Report, report_id), ctx["company"])
    ctx["report"] = report
    return render(request, "report.html", ctx)


@router.get("/invoices")
def invoices_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx, redirect = _context_or_redirect(request, db, user)
    if redirect:
        return redirect
    company = ctx["company"]
    today = local_date(utcnow(), company.timezone)
    rows = []
    for invoice in db.scalars(
        select(Invoice).where(Invoice.company_id == company.id, Invoice.is_open.is_(True))
    ).all():
        days = (today - invoice.due_date).days
        rows.append({"invoice": invoice, "days": days, "bucket": aging_bucket(days)})
    rows.sort(key=lambda row: (-row["days"], -row["invoice"].balance_cents))
    ctx.update(rows=rows, flash=pop_flash(request))
    return render(request, "invoices.html", ctx)


def _set_dispute(request: Request, invoice_id: int, user: User, db: Session, disputed: bool):
    ctx = page_context(request, db, user)
    invoice = scoped_or_404(db.get(Invoice, invoice_id), ctx["company"])
    set_disputed(db, invoice, disputed)
    set_flash(request, f"Invoice {invoice.doc_number} {'marked disputed' if disputed else 'unmarked'}.")
    return RedirectResponse("/invoices", status_code=303)


@router.post("/invoices/{invoice_id}/dispute")
def mark_disputed(invoice_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _set_dispute(request, invoice_id, user, db, True)


@router.post("/invoices/{invoice_id}/undispute")
def unmark_disputed(
    invoice_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return _set_dispute(request, invoice_id, user, db, False)


# ---------------------------------------------------------------------------
# Add-backs
# ---------------------------------------------------------------------------


def _addbacks_context(request: Request, db: Session, user: User, error: str | None = None) -> dict:
    ctx = page_context(request, db, user)
    company = ctx["company"]
    kpis = compute_kpis(db, company.id, report_date_for(local_date(utcnow(), company.timezone)))
    ctx.update(
        addbacks=db.scalars(select(Addback).where(Addback.company_id == company.id).order_by(Addback.id)).all(),
        categories=CATEGORY_LABELS,
        figures=display(kpis),
        kpis=kpis,
        error=error,
        flash=pop_flash(request),
        fmt_dollars=fmt_dollars,
    )
    return ctx


@router.get("/addbacks")
def addbacks_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx, redirect = _context_or_redirect(request, db, user)
    if redirect:
        return redirect
    return render(request, "addbacks.html", _addbacks_context(request, db, user))


def _parse_addback(category: str, label: str, amount: str, note: str) -> tuple[dict, str | None]:
    label, note = label.strip(), note.strip()
    if category not in {c.value for c in AddbackCategory}:
        return {}, "Choose a category."
    if not label or len(label) > 80:
        return {}, "Label is required and must be 80 characters or fewer."
    if len(note) > 200:
        return {}, "Note must be 200 characters or fewer."
    try:
        cents = parse_dollars(amount)
    except ValueError as exc:
        return {}, str(exc)
    if cents < 0:
        return {}, "Yearly amount must be 0 or more."
    return {"category": category, "label": label, "annual_amount_cents": cents, "note": note}, None


@router.post("/addbacks")
def add_addback(
    request: Request,
    category: str = Form(""),
    label: str = Form(""),
    amount: str = Form(""),
    note: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    ctx = page_context(request, db, user)
    values, error = _parse_addback(category, label, amount, note)
    if error:
        return render(request, "addbacks.html", _addbacks_context(request, db, user, error), status_code=400)
    db.add(Addback(company_id=ctx["company"].id, **values))
    db.commit()
    set_flash(request, "Add-back saved.")
    return RedirectResponse("/addbacks", status_code=303)


@router.post("/addbacks/{addback_id}")
def edit_addback(
    addback_id: int,
    request: Request,
    category: str = Form(""),
    label: str = Form(""),
    amount: str = Form(""),
    note: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    ctx = page_context(request, db, user)
    addback = scoped_or_404(db.get(Addback, addback_id), ctx["company"])
    values, error = _parse_addback(category, label, amount, note)
    if error:
        return render(request, "addbacks.html", _addbacks_context(request, db, user, error), status_code=400)
    for name, value in values.items():
        setattr(addback, name, value)
    db.commit()
    set_flash(request, "Add-back saved.")
    return RedirectResponse("/addbacks", status_code=303)


@router.post("/addbacks/{addback_id}/delete")
def delete_addback(
    addback_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    ctx = page_context(request, db, user)
    addback = scoped_or_404(db.get(Addback, addback_id), ctx["company"])
    db.delete(addback)
    db.commit()
    set_flash(request, "Add-back deleted.")
    return RedirectResponse("/addbacks", status_code=303)
