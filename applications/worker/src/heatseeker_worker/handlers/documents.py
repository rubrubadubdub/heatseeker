"""Versioned document/image evidence processing jobs."""

from heatseeker_common.job_registry import JobContext, PermanentJobError, job_handler
from heatseeker_source_registry.document_pipeline import process_document


@job_handler("documents.process")
def process_evidence_document(ctx: JobContext) -> dict:
    document_id = ctx.payload.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise PermanentJobError("documents.process requires document_id")
    try:
        return process_document(ctx.engine, ctx.settings, document_id)
    except LookupError as exc:
        raise PermanentJobError(str(exc)) from exc
