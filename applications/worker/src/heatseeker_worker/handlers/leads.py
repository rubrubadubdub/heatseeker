"""Lead rescoring jobs (M8)."""

from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobContext, PermanentJobError, job_handler
from heatseeker_lead_intelligence.service import rescore_offering


@job_handler("leads.rescore")
def rescore(ctx: JobContext) -> dict:
    offering_id = ctx.payload.get("offering_id")
    if not isinstance(offering_id, str) or not offering_id:
        raise PermanentJobError("leads.rescore requires offering_id")
    with session_scope(ctx.engine) as session:
        try:
            return rescore_offering(session, offering_id, actor="worker")
        except LookupError as exc:
            raise PermanentJobError(str(exc)) from exc
