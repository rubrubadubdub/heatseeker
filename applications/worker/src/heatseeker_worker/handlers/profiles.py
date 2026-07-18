"""Deterministic company-website profile fetch jobs (AI-free enrichment)."""

from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobContext, PermanentJobError, job_handler
from heatseeker_intelligence.company_profiles import fetch_and_extract


@job_handler("profiles.fetch")
def profile_fetch(ctx: JobContext) -> dict:
    organisation_id = ctx.payload.get("organisation_id")
    if not isinstance(organisation_id, str) or not organisation_id:
        raise PermanentJobError("profiles.fetch requires organisation_id")
    with session_scope(ctx.engine) as session:
        try:
            return fetch_and_extract(session, ctx.settings, organisation_id)
        except LookupError as exc:
            raise PermanentJobError(str(exc)) from exc
