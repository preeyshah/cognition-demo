import csv
import io

from app.models import AuditLog

from .conftest import auth, make_refund, user

ADMIN = "priya.admin@acmepay.com"
VIEWER = "vik.auditor@acmepay.com"


def test_export_refunds_csv_handles_refunds_without_escalation(client, db):
    admin = user(db, ADMIN)
    r = make_refund(db, None)
    assert r.escalation_reason is None
    res = client.get("/admin/export/refunds.csv", headers=auth(admin))
    assert res.status_code == 200
    rows = {row["reference"]: row for row in csv.DictReader(io.StringIO(res.text))}
    assert rows[r.reference]["escalation_reason"] == ""
    assert db.query(AuditLog).filter_by(action="export.refunds").count() == 1


def test_export_refunds_csv_strips_escalation_reason(client, db):
    admin = user(db, ADMIN)
    r = make_refund(db, None)
    r.escalation_reason = "  Above my approval limit \n"
    db.commit()
    res = client.get("/admin/export/refunds.csv", headers=auth(admin))
    rows = {row["reference"]: row for row in csv.DictReader(io.StringIO(res.text))}
    assert rows[r.reference]["escalation_reason"] == "Above my approval limit"


def test_export_refunds_csv_is_admin_only(client, db):
    assert client.get("/admin/export/refunds.csv", headers=auth(user(db, VIEWER))).status_code == 403
