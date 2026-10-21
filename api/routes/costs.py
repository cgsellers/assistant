"""
GET /costs -- extraction spend, from the rows that already record it.

Behind the API key like everything else, with no change needed here: the key
check is applied to the app, not per router, so this route was protected the
moment it existed.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.costs import cost_report
from core.db import get_session

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("", summary="Extraction spend in a date window")
def get_costs(
    since: date | None = Query(None, description="Inclusive, YYYY-MM-DD"),
    until: date | None = Query(None, description="Inclusive, YYYY-MM-DD"),
    session: Session = Depends(get_session),
) -> dict:
    return cost_report(session, since=since, until=until).as_dict()
