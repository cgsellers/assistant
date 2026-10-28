import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes import auth, costs, documents, processors, settings
from core import migrations
from core.auth import require_principal
from core.config import get_settings
from core.db import get_session
from doctypes.authoring import AuthoringError
from panel import routes as panel

log = logging.getLogger("api")

# Applied to the whole app, not to each router. A route added later is then
# protected because nobody did anything, rather than exposed because somebody
# forgot; opening one means editing core.auth.PUBLIC_PATHS, which is a line a
# reviewer will see in a diff.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not get_settings().require_auth:
        log.warning(
            "REQUIRE_AUTH is off: every endpoint is open. Local development "
            "only -- there is no owner model, so any caller sees everything."
        )
    yield


app = FastAPI(
    title="Ledgerline",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(require_principal)],
)

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(settings.router)
app.include_router(costs.router)
app.include_router(processors.router)
app.include_router(panel.router)

# A mount sits outside the app-wide dependency, so these files are public --
# which is right: HTMX and a stylesheet, nothing that needs a credential.
app.mount("/panel/static", StaticFiles(directory=panel.STATIC_DIR), name="panel-static")
app.middleware("http")(panel.security_headers)
app.add_exception_handler(StarletteHTTPException, panel.unauthenticated)


async def _authoring_error(request: Request, exc: AuthoringError):
    """One handler: pages for the panel, JSON for the API."""
    if request.url.path.startswith("/panel"):
        return panel.authoring_page(request, exc)
    return processors.authoring_error(request, exc)


app.add_exception_handler(AuthoringError, _authoring_error)


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    """Liveness: the process is up and the database answers. Nothing more."""
    session.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}


@app.get("/ready")
def ready(session: Session = Depends(get_session)) -> JSONResponse:
    """
    Readiness: the database is at the migration this code expects.

    The check `/health` cannot make. An empty or half-migrated database
    passes `/health`, and then every real request fails (DESIGN §8.2), so a
    load balancer or orchestrator should route traffic on this one. 503 with
    the reason until the schema matches.
    """
    reason = migrations.problem(session.get_bind())
    if reason is not None:
        return JSONResponse({"status": "not ready", "reason": reason}, status_code=503)
    return JSONResponse({"status": "ready", "revision": migrations.expected_revision()})