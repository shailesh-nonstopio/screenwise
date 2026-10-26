"""Email and AI provider failure handling (no network: transports are mocked)."""

import smtplib

import httpx
import pytest

from app.database import db_session
from app.models import Application, Invitation, User
from app.services import email_service
from app.services.applications import issue_invitation
from app.services.llm_provider import AnthropicProvider, LLMError, OpenAIProvider, _extract_json


def test_smtp_failure_is_recorded_not_hidden(client, application_id, test_settings, monkeypatch):
    test_settings.email_backend = "smtp"
    test_settings.smtp_host = "smtp.invalid"

    class Boom:
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, "unavailable")

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    with db_session() as db:
        app = db.get(Application, application_id)
        inv, _, rec = issue_invitation(db, app, db.query(User).first())
        assert rec.status == "failed" and "SMTP error" in rec.error
        assert db.get(Invitation, inv.id).delivery_status == "failed" and inv.sent_at is None


def test_smtp_success_marks_sent(client, application_id, test_settings, monkeypatch):
    test_settings.email_backend = "smtp"
    test_settings.smtp_host = "smtp.example.com"
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg); return {}

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with db_session() as db:
        app = db.get(Application, application_id)
        inv, token, rec = issue_invitation(db, app, db.query(User).first())
        assert rec.status == "sent" and inv.sent_at is not None
    assert token in sent[0].get_body(("plain",)).get_content()  # the candidate gets the real link
    assert "camera and microphone" in sent[0].get_body(("plain",)).get_content().lower()


def test_resend_rejection_is_recorded(client, application_id, test_settings, monkeypatch):
    test_settings.email_backend = "resend"
    test_settings.resend_api_key = "re_test"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(422, json={"message": "invalid from"}))
    res = email_service.ResendBackend().send(email_service.OutgoingEmail("a@example.com", "s", "t", "<p>h</p>"))
    assert res.status == "failed" and "invalid from" in res.error


def test_missing_api_key_is_reported(test_settings):
    test_settings.llm_provider = "openai"
    test_settings.openai_api_key = ""
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        OpenAIProvider()


def test_openai_malformed_reply_raises(test_settings, monkeypatch):
    test_settings.openai_api_key = "sk-test"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]}))
    with pytest.raises(LLMError):
        OpenAIProvider().complete_json("s", "u")


def test_rate_limit_then_timeout_raises_after_retries(test_settings, monkeypatch):
    test_settings.anthropic_api_key = "k"
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(LLMError, match="timed out"):
        AnthropicProvider().complete_json("s", "u")
    assert calls["n"] == 3


def test_extract_json_handles_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Here you go: {"a": 2} thanks') == {"a": 2}
    with pytest.raises(LLMError):
        _extract_json("[1, 2]")


def test_interview_continues_when_ai_is_down(client, application_id, test_settings, monkeypatch):
    """If the provider fails mid-interview, the candidate still gets the next planned question."""
    from app.services.interview_engine import start_session, submit_answer
    from app.services.llm_provider import LLMProvider

    class Down(LLMProvider):
        name = "down"

        def assess_answer(self, ctx):
            raise LLMError("Provider timed out")

        def clarify(self, ctx):
            raise LLMError("Provider timed out")

    with db_session() as db:
        app = db.get(Application, application_id)
        inv, _, _ = issue_invitation(db, app, db.query(User).first())
        sess = start_session(db, app, inv)
        submit_answer(db, sess, "short", "z1", Down())
        assert sess.current_index == 1 and sess.messages[-1].kind == "question"
        from app.services.interview_engine import request_clarification

        request_clarification(db, sess, None, "z2", Down())
        assert sess.messages[-1].kind == "clarification" and sess.messages[-1].content  # planned fallback text
