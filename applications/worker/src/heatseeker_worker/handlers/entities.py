"""Entity resolution jobs (M4)."""

from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobContext, job_handler
from heatseeker_entity_resolution.matching import scan_for_duplicates


@job_handler("entities.match_scan")
def match_scan(ctx: JobContext) -> dict:
    with session_scope(ctx.engine) as session:
        return scan_for_duplicates(session)
