import logging
from datetime import timedelta

from sqlalchemy import inspect, text

from app.database import db_session, get_engine
from app.models import Application, EmailMessage, Invitation, User, utcnow
from app.security import hash_token
from app.services.applications import InvitationError, issue_invitation, revoke_invitation


def _invite(app_id: int, **kw):
    with db_session() as db:
        app = db.get(Application, app_id)
        user = db.query(User).first()
        inv, token, record = issue_invitation(db, app, user, **kw)
        return inv.id, token, record.status


def test_token_is_long_random_and_only_its_hash_is_stored(client, application_id):
    _, token, _ = _invite(application_id)
    assert len(token) >= 43  # 256 bits, url-safe base64
    with db_session() as db:
        inv = db.query(Invitation).one()
        assert inv.token_hash == hash_token(token) and inv.token_hash != token
    # The plaintext token appears nowhere in the database.
    eng = get_engine()
    with eng.connect() as conn:
        for table in inspect(eng).get_table_names():
            for row in conn.execute(text(f"SELECT * FROM {table}")):
                assert token not in " ".join(str(v) for v in row), f"token leaked into {table}"


def test_dry_run_never_claims_email_was_sent(client, application_id, recruiter):
    _, token, status = _invite(application_id)
    assert status == "logged_not_sent"
    with db_session() as db:
        inv = db.query(Invitation).one()
        assert inv.sent_at is None and inv.delivery_status == "logged_not_sent"
        email = db.query(EmailMessage).one()
        assert "[personal interview link" in email.body_text and token not in email.body_text
    page = client.get(f"/recruiter/applications/{application_id}/invite").text
    assert "Logged (not sent)" in page
    assert "Invitation emailed" not in page


def test_duplicate_invitation_is_blocked(client, application_id):
    _invite(application_id)
    try:
        _invite(application_id)
        raise AssertionError("second active invitation should be refused")
    except InvitationError as exc:
        assert "already exists" in exc.message
    # Replacing revokes the old link first.
    _invite(application_id, replace=True)
    with db_session() as db:
        invs = db.query(Invitation).all()
        assert sum(1 for i in invs if i.is_active) == 1


def test_expired_and_revoked_links_are_refused(client, application_id):
    inv_id, token, _ = _invite(application_id)
    with db_session() as db:
        inv = db.get(Invitation, inv_id)
        inv.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    r = client.get(f"/interview/{token}")
    assert r.status_code == 410 and "expired" in r.text

    inv2_id, token2, _ = _invite(application_id, replace=True)
    with db_session() as db:
        revoke_invitation(db, db.get(Invitation, inv2_id), db.query(User).first())
    r = client.get(f"/interview/{token2}")
    assert r.status_code == 410 and "can't be used" in r.text


def test_unknown_token_shows_not_found(client):
    r = client.get("/interview/" + "x" * 43)
    assert r.status_code == 404 and "can't find this interview link" in r.text


def test_token_is_exchanged_and_removed_from_url(client, application_id):
    _, token, _ = _invite(application_id)
    r = client.get(f"/interview/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/interview"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_token_is_not_logged(client, application_id, caplog):
    _, token, _ = _invite(application_id)
    with caplog.at_level(logging.INFO):
        client.get(f"/interview/{token}")
    from app.logging_config import JsonFormatter, RedactingFilter

    fmt, flt = JsonFormatter(), RedactingFilter()
    for rec in caplog.records:
        flt.filter(rec)
        assert token not in fmt.format(rec)


def test_revoke_requires_recruiter(client, application_id):
    inv_id, _, _ = _invite(application_id)
    r = client.post(f"/recruiter/invitations/{inv_id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
