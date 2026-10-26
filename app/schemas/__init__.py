"""Pydantic schemas used to validate untrusted model output and request payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Rating = Literal[1, 2, 3, 4]
RatingStatus = Literal["rated", "not_assessed", "insufficient_evidence"]
Recommendation = Literal["recommend_proceeding", "proceed_with_follow_up", "additional_assessment", "insufficient_evidence"]

RECOMMENDATION_LABELS: dict[str, str] = {
    "recommend_proceeding": "Recommend proceeding",
    "proceed_with_follow_up": "Proceed with follow-up",
    "additional_assessment": "Additional assessment recommended",
    "insufficient_evidence": "Insufficient evidence",
}

DECISION_LABELS: dict[str, str] = {
    "advance": "Advance",
    "hold": "Hold / Follow up",
    "decline": "Decline",
    "undecided": "Not yet decided",
}


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _clip(v: str | None, n: int) -> str:
    return (v or "").strip()[:n]


# --------------------------------------------------------------------------- resume


class ParsedRole(_Loose):
    title: str = ""
    organization: str | None = None
    start: str | None = None
    end: str | None = None
    start_certain: bool = False
    end_certain: bool = False
    ref: str | None = None
    uncertain_note: str = ""

    @field_validator("title", "uncertain_note")
    @classmethod
    def _c(cls, v: str) -> str:
        return _clip(v, 200)


class RefText(_Loose):
    text: str
    ref: str | None = None
    year: str | None = None

    @field_validator("text")
    @classmethod
    def _c(cls, v: str) -> str:
        return _clip(v, 300)


class ExperienceYears(_Loose):
    min: float = Field(ge=0, le=60)
    max: float = Field(ge=0, le=60)
    certain: bool = False
    note: str = ""


class ParsedResume(_Loose):
    roles: list[ParsedRole] = Field(default_factory=list, max_length=30)
    skills: list[str] = Field(default_factory=list, max_length=80)
    education: list[RefText] = Field(default_factory=list, max_length=15)
    achievements: list[RefText] = Field(default_factory=list, max_length=30)
    projects: list[RefText] = Field(default_factory=list, max_length=30)
    experience_years: ExperienceYears | None = None
    unknowns: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s = _clip(s, 60)
            if s and s.lower() not in {x.lower() for x in out}:
                out.append(s)
        return out


# --------------------------------------------------------------------------- planning


class DraftQuestion(_Loose):
    criterion_id: str | None = None
    behavior_id: str | None = None
    category: Literal["role_skills", "depth", "problem_solving", "communication", "workplace"] = "role_skills"
    text: str = Field(min_length=10, max_length=600)
    clarification: str = Field(default="", max_length=600)


class DraftQuestions(_Loose):
    questions: list[DraftQuestion] = Field(min_length=1, max_length=20)


class QuestionText(_Loose):
    text: str = Field(min_length=10, max_length=600)
    clarification: str = Field(default="", max_length=600)


class AnswerAssessment(_Loose):
    action: Literal["next", "follow_up"]
    follow_up: str | None = Field(default=None, max_length=400)
    reason: str = Field(default="", max_length=300)


class Clarification(_Loose):
    text: str = Field(min_length=5, max_length=700)


# --------------------------------------------------------------------------- report


class Cited(_Loose):
    text: str = Field(max_length=1200)
    refs: list[str] = Field(default_factory=list, max_length=12)


class GapItem(Cited):
    kind: Literal["not_demonstrated", "not_asked", "unverified"] = "not_demonstrated"


class CriterionRating(_Loose):
    criterion_id: str
    rating: Rating | None = None
    status: RatingStatus = "rated"
    evidence: str = Field(default="", max_length=1500)
    gap: str = Field(default="", max_length=600)
    refs: list[str] = Field(default_factory=list, max_length=12)


class QuestionRating(_Loose):
    q: str
    rating: Rating | None = None
    status: RatingStatus = "rated"
    explanation: str = Field(default="", max_length=1500)
    refs: list[str] = Field(default_factory=list, max_length=12)


class BehaviorRating(_Loose):
    behavior_id: str
    rating: Rating | None = None
    status: RatingStatus = "rated"
    note: str = Field(default="", max_length=800)
    refs: list[str] = Field(default_factory=list, max_length=12)


class CrossCheck(_Loose):
    supported: list[Cited] = Field(default_factory=list, max_length=15)
    needs_clarification: list[Cited] = Field(default_factory=list, max_length=15)
    not_covered: list[Cited] = Field(default_factory=list, max_length=15)


class FollowUp(_Loose):
    kind: Literal["exercise", "technical_interview", "recruiter_question"] = "recruiter_question"
    text: str = Field(max_length=800)


class Report(_Loose):
    executive_summary: str = Field(max_length=3000)
    summary_refs: list[str] = Field(default_factory=list, max_length=20)
    criteria: list[CriterionRating] = Field(default_factory=list, max_length=30)
    questions: list[QuestionRating] = Field(default_factory=list, max_length=40)
    strengths: list[Cited] = Field(default_factory=list, max_length=15)
    gaps: list[GapItem] = Field(default_factory=list, max_length=20)
    resume_crosscheck: CrossCheck = Field(default_factory=CrossCheck)
    behaviors: list[BehaviorRating] = Field(default_factory=list, max_length=10)
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=10)
    recommendation: Recommendation
    recommendation_rationale: str = Field(default="", max_length=1500)
