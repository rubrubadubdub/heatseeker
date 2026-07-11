"""Source vetting, grading, auto-deprecation, and reinstatement.

Grades are computed from observed evidence only — a source with no fetch history is
"U" (ungraded), never guessed (spec §6.3). Auto-deprecation is deterministic, always
records its reason, is fully audited, and is reversible via reinstate().
"""

from datetime import timedelta

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceLifecycle,
    TermsStatus,
)

GRADE_BANDS = [(85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "E")]
MIN_ATTEMPTS_TO_GRADE = 2
DEPRECATE_FAILURE_STREAK = 8
DEPRECATE_STALE_SUCCESS_DAYS = 30
DEPRECATE_SCORE_BELOW = 25.0
DEPRECATE_SCORE_MIN_ATTEMPTS = 6


def compute_grade(source: SourceDefinition) -> tuple[float | None, str, dict]:
    """Return (score 0-100 | None, letter A-E or 'U', component detail)."""
    if source.fetch_attempts < MIN_ATTEMPTS_TO_GRADE:
        return None, "U", {"reason": "insufficient fetch history — abstaining (spec §6.3)"}

    # Reliability (0-35): success ratio, dented by an active failure streak.
    success_ratio = source.fetch_successes / source.fetch_attempts
    reliability = 35.0 * success_ratio * (0.5 ** min(source.consecutive_failures, 6))

    # Yield (0-25): does collecting this source produce new material?
    yield_events = source.docs_new + source.docs_unchanged
    novelty_ratio = source.docs_new / yield_events if yield_events else 0.0
    yield_score = 25.0 * (0.3 + 0.7 * novelty_ratio) if yield_events else 0.0

    # Policy cleanliness (0-20): reviewed + permitted beats unknown.
    if source.terms_status == TermsStatus.PROHIBITED or (
        source.robots_status == RobotsStatus.DISALLOWED
    ):
        policy = 0.0
    else:
        robots_part = {
            RobotsStatus.ALLOWED: 10.0,
            RobotsStatus.NOT_APPLICABLE: 10.0,
            RobotsStatus.UNKNOWN: 4.0,
            RobotsStatus.UNREACHABLE: 2.0,
        }.get(source.robots_status, 0.0)
        terms_part = {
            TermsStatus.APPROVED: 10.0,
            TermsStatus.UNREVIEWED: 5.0,
            TermsStatus.UNCLEAR: 3.0,
        }.get(source.terms_status, 0.0)
        policy = robots_part + terms_part

    # Authority (0-20): tier 1 official record → 20 … tier 7 weak → ~2.9.
    authority = 20.0 * (8 - min(max(source.authority_tier, 1), 7)) / 7

    score = round(reliability + yield_score + policy + authority, 1)
    letter = next(band for threshold, band in GRADE_BANDS if score >= threshold)
    detail = {
        "reliability": round(reliability, 1),
        "yield": round(yield_score, 1),
        "policy": round(policy, 1),
        "authority": round(authority, 1),
        "success_ratio": round(success_ratio, 3),
        "novelty_ratio": round(novelty_ratio, 3),
        "attempts": source.fetch_attempts,
    }
    return score, letter, detail


def deprecation_reason(source: SourceDefinition) -> str | None:
    """Deterministic auto-deprecation rules. None = keep."""
    if source.robots_status == RobotsStatus.DISALLOWED:
        return "robots.txt disallows collection (recheck confirmed)"
    if source.terms_status == TermsStatus.PROHIBITED:
        return "terms of use prohibit automated access"
    if source.consecutive_failures >= DEPRECATE_FAILURE_STREAK:
        stale_cutoff = utc_now() - timedelta(days=DEPRECATE_STALE_SUCCESS_DAYS)
        if source.last_success_at is None or source.last_success_at < stale_cutoff:
            return (
                f"persistently failing ({source.consecutive_failures} consecutive) with no "
                f"success in {DEPRECATE_STALE_SUCCESS_DAYS} days"
            )
    if (
        source.quality_score is not None
        and source.quality_score < DEPRECATE_SCORE_BELOW
        and source.fetch_attempts >= DEPRECATE_SCORE_MIN_ATTEMPTS
    ):
        return f"sustained grade E (score {source.quality_score} over {source.fetch_attempts} attempts)"
    return None


def evaluate_source(session: Session, source: SourceDefinition) -> dict:
    """Grade one source; auto-deprecate active/degraded/candidate sources that fail the rules."""
    score, letter, detail = compute_grade(source)
    source.quality_score = score
    source.quality_grade = letter
    source.grade_detail = detail
    source.graded_at = utc_now()
    source.health_score = (score / 100.0) if score is not None else None

    outcome = {"source_id": source.id, "name": source.name, "score": score, "grade": letter}
    if source.lifecycle_status in (
        SourceLifecycle.ACTIVE,
        SourceLifecycle.DEGRADED,
        SourceLifecycle.CANDIDATE,
    ):
        reason = deprecation_reason(source)
        if reason:
            source.lifecycle_status = SourceLifecycle.DEPRECATED
            source.deprecated_at = utc_now()
            source.deprecation_reason = reason
            audit.record(
                session,
                "evaluator",
                "source.auto_deprecated",
                "source",
                source.id,
                {"name": source.name, "reason": reason, "score": score, "grade": letter},
            )
            outcome["deprecated"] = reason
    source.updated_at = utc_now()
    return outcome


def evaluate_all(session: Session) -> dict:
    """Grade every non-rejected source; apply auto-deprecation. Returns a summary."""
    sources = list(
        session.scalars(
            select(SourceDefinition).where(
                SourceDefinition.lifecycle_status != SourceLifecycle.REJECTED
            )
        )
    )
    deprecated: list[str] = []
    grades: dict[str, int] = {}
    for source in sources:
        outcome = evaluate_source(session, source)
        grades[outcome["grade"]] = grades.get(outcome["grade"], 0) + 1
        if "deprecated" in outcome:
            deprecated.append(f"{outcome['name']}: {outcome['deprecated']}")
    audit.record(
        session,
        "evaluator",
        "sources.evaluated",
        None,
        None,
        {"count": len(sources), "grades": grades, "deprecated": deprecated},
    )
    return {"evaluated": len(sources), "grades": grades, "deprecated": deprecated}


def reinstate(session: Session, source_id: str, actor: str = "user") -> SourceDefinition | None:
    """Reverse a deprecation: back to candidate with counters reset; fully audited."""
    source = session.get(SourceDefinition, source_id)
    if source is None or source.lifecycle_status != SourceLifecycle.DEPRECATED:
        return None
    previous_reason = source.deprecation_reason
    source.lifecycle_status = SourceLifecycle.CANDIDATE
    source.deprecated_at = None
    source.deprecation_reason = None
    source.consecutive_failures = 0
    source.retry_after_until = None
    source.updated_at = utc_now()
    audit.record(
        session,
        actor,
        "source.reinstated",
        "source",
        source.id,
        {"name": source.name, "previous_reason": previous_reason},
    )
    return source
