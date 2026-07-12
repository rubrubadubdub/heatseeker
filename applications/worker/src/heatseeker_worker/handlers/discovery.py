"""Bulk-dataset discovery import jobs (M5)."""

from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobContext, PermanentJobError, job_handler
from heatseeker_common.timeutil import utc_now
from heatseeker_intelligence.discovery import execute_import, load_run_content
from heatseeker_intelligence.models import BulkImportRun, ImportRunStatus


@job_handler("discovery.import_csv")
def import_csv(ctx: JobContext) -> dict:
    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise PermanentJobError("discovery.import_csv requires run_id")
    try:
        with session_scope(ctx.engine) as session:
            run = session.get(BulkImportRun, run_id)
            if run is None:
                raise PermanentJobError(f"import run not found: {run_id}")
            content = load_run_content(ctx.settings, session, run)
            run = execute_import(session, ctx.settings, run_id, content)
            result = {
                "rows": run.row_count,
                "imported": run.imported_count,
                "matched_existing": run.matched_existing_count,
                "out_of_scope": run.skipped_out_of_scope_count,
                "rejected": run.rejected_count,
            }
        return result
    except Exception as exc:
        # The work transaction rolls back first. Persist failure in an independent
        # transaction so re-raising to the job runner cannot erase run diagnostics.
        with session_scope(ctx.engine) as failure_session:
            failed_run = failure_session.get(BulkImportRun, run_id)
            if failed_run is not None and failed_run.status != ImportRunStatus.SUCCEEDED:
                failed_run.status = ImportRunStatus.FAILED
                failed_run.error = str(exc)[:2000]
                failed_run.finished_at = utc_now()
        raise
