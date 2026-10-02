"""FastAPI app: settings check, middleware, routes and the scheduler."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.auth import CSRFMiddleware, LoginRequired, PasswordChangeRequired
from app.config import exit_if_invalid, get_settings
from app.db import init_engine
from app.routes import auth, operator, pages, qbo, reminders
from app.routes.common import render

SESSION_IDLE_SECONDS = 12 * 60 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    exit_if_invalid(settings)
    logging.basicConfig(level=settings.LOG_LEVEL.upper(), format="%(levelname)s %(name)s: %(message)s")
    init_engine(settings.DATABASE_URL)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.SCHEDULER_ENABLED:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        middleware=[
            # Outermost first: the session must exist before the CSRF check reads it.
            Middleware(
                SessionMiddleware,
                secret_key=settings.APP_SECRET_KEY,
                session_cookie="kpi_session",
                max_age=SESSION_IDLE_SECONDS,  # re-signed on every response, so this is an idle timeout
                same_site="lax",
                https_only=settings.PUBLIC_BASE_URL.startswith("https://"),
            ),
            Middleware(CSRFMiddleware),
        ],
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (auth, pages, reminders, qbo, operator):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        return RedirectResponse("/login", status_code=302)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change(request: Request, _exc: PasswordChangeRequired):
        return RedirectResponse("/account", status_code=302)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        messages = {403: "You don't have access to this page.", 404: "Not found."}
        message = exc.detail if exc.status_code == 400 else messages.get(exc.status_code, exc.detail)
        return render(request, "error.html", {"status": exc.status_code, "message": message}, status_code=exc.status_code)

    return app


app = create_app()
