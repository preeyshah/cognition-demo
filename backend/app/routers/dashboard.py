from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..analytics import analytics, summary_for
from ..db import get_db
from ..models import Role, User
from ..security import get_current_user, require_roles

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return summary_for(db, user)


@router.get("/analytics/overview")
def overview(
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(require_roles(Role.admin, Role.viewer)),
    db: Session = Depends(get_db),
):
    return analytics(db, user, days)
