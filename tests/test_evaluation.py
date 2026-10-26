from typing import Any

import pytest

from app.database import db_session
from app.models import Application, InterviewSession, User
from app.schemas import Report
from app.services.applications import issue_invitation
from app.services.evaluation import generate, normalise, queue_report
from app.services.interview_engine import complete, start_session, submit_answer
from app.services.llm_provider import LLMError, LLMProvider
from app.services.mock_llm import MockProvider

ANS = "I personally led this because the previous approach failed; I compared options, chose one, and as a result errors dropped 30% in a month."


def _ctx(answered: int = 3) -> dict[str, Any]:
    return {
        "job": {
            "essential_criteria": [{"id": "api", "name": "APIs"}, {"id": "sql", "name": "SQL"}],
            "behaviors": [{"id": "collaboration", "name": "Collaboration", "enabled": True}],
        },
        "answers": [
            {"q": f"Q{i + 1}", "texts": ["answer"] if i < answered else [], "criterion_id": ["api", "sql", None][i], "behavior_id": [None, None, "collaboration"][i], "scored": True}
            for i in range(3)
        ],
        "resume_lines": [{"ref": "R-1", "text": "Built APIs"}],
    }


def _report(**over) -> Report:
    base = {
        "executive_summary": "Summary.",
        "criteria": [{"criterion_id": "api", "rating": 4, "status": "rated", "evidence": "x", "refs": ["Q1", "R1"]}],
        "questions": [{"q": "q1", "rating": 4, "status": "rated", "refs": ["Q1"]}],
        "strengths": [{"text": "unsupported strength", "refs": []}, {"text": "ok", "refs": ["Q1"]}],
        "behaviors": [],
        "recommendation": "recommend_proceeding",
    }
    base.update(over)
    return Report.model_validate(base)


def test_missing_essential_criterion_is_not_hidden():
    data = normalise(_report(), _ctx())
    sql = next(c for c in data["criteria"] if c["criterion_id"] == "sql")
    assert sql["status"] == "not_assessed" and sql["rating"] is None
    # An unmet essential criterion blocks an unqualified "recommend proceeding".
    assert data["recommendation"] == "proceed_with_follow_up"
    assert data["policy_adjustments"]


def test_ratings_without_valid_evidence_are_withheld():
    rep = _report(criteria=[{"criterion_id": "api", "rating": 3, "status": "rated", "refs": ["Q9", "R-77"]}])
    data = normalise(rep, _ctx())
    api = data["criteria"][0]
    assert api["rating"] is None and api["status"] == "insufficient_evidence" and api["refs"] == []
    assert [s["text"] for s in data["strengths"]] == ["ok"]  # unreferenced strength dropped


def test_refs_are_normalised_and_unknown_criteria_dropped():
    rep = _report(criteria=[
        {"criterion_id": "api", "rating": 4, "status": "rated", "refs": ["r1", "q1"]},
        {"criterion_id": "made_up", "rating": 4, "status": "rated", "refs": ["Q1"]},
    ])
    data = normalise(rep, _ctx())
    assert [c["criterion_id"] for c in data["criteria"]] == ["api", "sql"]
    assert data["criteria"][0]["refs"] == ["R-1", "Q1"]
    assert data["questions"][0]["q"] == "Q1"


def test_too_few_answers_forces_insufficient_evidence():
    data = normalise(_report(), _ctx(answered=1))
    assert data["recommendation"] == "insufficient_evidence"


def test_behavior_without_answer_is_not_assessed():
    data = normalise(_report(), _ctx(answered=2))
    assert data["behaviors"][0]["status"] == "not_assessed"


def test_mock_report_never_uses_numeric_score_for_thin_answers():
    from app.services.mock_llm import heuristic_rating

    assert heuristic_rating("too short", {"api"}) == (None, "insufficient_evidence")
    assert heuristic_rating("", {"api"}) == (None, "not_assessed")


@pytest.fixture
def completed_session(client, application_id) -> int:
    with db_session() as db:
        app = db.get(Application, application_id)
        inv, _, _ = issue_invitation(db, app, db.query(User).first())
        sess = start_session(db, app, inv)
        for i in range(30):
            if sess.current_index >= len(sess.plan_json["questions"]):
                break
            submit_answer(db, sess, ANS, f"m{i}", MockProvider())
        complete(db, sess)
        return sess.id


class BrokenJSON(LLMProvider):
    name, model = "broken", "broken-1"

    def evaluate(self, ctx):
        return {"executive_summary": 42, "recommendation": "hire them"}


class Down(LLMProvider):
    name, model = "down", "down-1"

    def evaluate(self, ctx):
        raise LLMError("Provider timed out")


def test_malformed_model_output_fails_safely_and_can_be_retried(completed_session):
    with db_session() as db:
        sess = db.get(InterviewSession, completed_session)
        report = generate(db, queue_report(db, sess), BrokenJSON())
        assert report.status == "failed" and "format" in report.error
        assert report.report_json == {}  # nothing fabricated
        report = generate(db, queue_report(db, sess), Down())
        assert report.status == "failed" and "timed out" in report.error
        report = generate(db, queue_report(db, sess), MockProvider())
        assert report.status == "ready" and report.attempts == 3


def test_report_page_separates_ai_and_human_decision(client, recruiter, completed_session, application_id):
    with db_session() as db:
        sess = db.get(InterviewSession, completed_session)
        generate(db, queue_report(db, sess), MockProvider())
    page = client.get(f"/recruiter/applications/{application_id}/report").text
    assert "AI recommendation · advisory" in page
    assert "Recorded separately from the AI" in page
    assert "Development mock report" in page
    r = client.post(f"/recruiter/applications/{application_id}/decision", data={"decision": "hold", "notes": "Ask about SQL", "csrf_token": recruiter["csrf"]}, follow_redirects=False)
    assert r.status_code == 303
    export = client.get(f"/recruiter/applications/{application_id}/report.json").json()
    assert export["human_decision"]["decision"] == "hold"
    assert export["ai_report_advisory"]["recommendation"]
