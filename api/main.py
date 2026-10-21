import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.routes import costs, documents
from core.auth import require_api_key
from core.config import get_settings
from core.db import get_session

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
    dependencies=[Depends(require_api_key)],
)

app.include_router(documents.router)
app.include_router(costs.router)


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}