import csv
import io

from app.models import AuditLog

from .conftest import auth, make_refund, user

ADMIN = "priya.admin@acmepay.com"
JORDAN = "jordan.lee@acmepay.com"


def test_export_handles_never_escalated_refunds(client, db):
    r = make_refund(db, user(db, JORDAN))
    assert r.escalation_reason is None
    res = client.get("/admin/export/refunds.csv", headers=auth(user(db, ADMIN)))
    assert res.status_code == 200
    rows = {row["reference"]: row for row in csv.DictReader(io.StringIO(res.text))}
    assert rows[r.reference]["escalation_reason"] == ""
    assert rows[r.reference]["escalated_by"] == ""
    assert db.query(AuditLog).filter_by(action="export.refunds").count() == 1


def test_export_trims_escalation_reason(client, db):
    r = make_refund(db, user(db, JORDAN))
    r.escalation_reason = "  Above my approval limit  \n"
    db.commit()
    res = client.get("/admin/export/refunds.csv", headers=auth(user(db, ADMIN)))
    rows = {row["reference"]: row for row in csv.DictReader(io.StringIO(res.text))}
    assert rows[r.reference]["escalation_reason"] == "Above my approval limit"


def test_export_is_admin_only(client, db):
    assert client.get("/admin/export/refunds.csv", headers=auth(user(db, JORDAN))).status_code == 403
