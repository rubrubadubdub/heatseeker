"""Agentic source-scout jobs."""

from heatseeker_ai.models import ProposalStatus, SourceProposal
from heatseeker_ai.providers import ScoutCancelled
from heatseeker_ai.service import activate_proposal, execute_run
from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobCancelled, JobContext, PermanentJobError, job_handler


@job_handler("source_scout.run")
def run_source_scout(ctx: JobContext) -> dict:
    try:
        return execute_run(ctx.engine, ctx.settings, ctx.payload["run_id"])
    except ScoutCancelled as exc:
        raise JobCancelled(str(exc)) from exc


@job_handler("source_scout.activate_proposal")
def activate_source_proposal(ctx: JobContext) -> dict:
    proposal_id = ctx.payload["proposal_id"]
    with session_scope(ctx.engine) as session:
        proposal = session.get(SourceProposal, proposal_id)
        if proposal is None:
            raise PermanentJobError(f"source proposal not found: {proposal_id}")
        if proposal.status != ProposalStatus.PROPOSED:
            raise PermanentJobError(f"source proposal is no longer actionable: {proposal.status}")
        activated = activate_proposal(session, ctx.settings, proposal, actor="worker")
        note = proposal.review_note
    if not activated:
        raise PermanentJobError(note or "proposal did not clear policy gates")
    return {"proposal_id": proposal_id, "activated": True}
