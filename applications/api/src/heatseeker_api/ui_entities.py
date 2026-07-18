"""GUI routes for entity resolution and evidence-backed company profiles."""

import json
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.matching import scan_for_duplicates
from heatseeker_entity_resolution.models import (
    EntityMerge,
    IdentifierScheme,
    OrganisationType,
)
from heatseeker_entity_resolution.resolution import (
    ResolutionError,
    list_queue,
    perform_merge,
    record_decision,
    reverse_merge,
)
from heatseeker_intelligence import gaps
from heatseeker_intelligence import profile as intelligence_profile
from heatseeker_intelligence.models import ExtractionMethod
from heatseeker_intelligence.observations import record_observation
from heatseeker_knowledge_graph import graph as knowledge_graph
from heatseeker_knowledge_graph.models import RELATIONSHIP_TYPES
from heatseeker_knowledge_graph.projects import participations_for_organisation
from heatseeker_lead_intelligence.service import leads_for_organisation
from heatseeker_source_registry.models import SourceDocument
from sqlalchemy import select

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)

MATCH_STATE_BADGES = {
    "exact": "danger",
    "high_confidence_probable": "warning",
    "possible_review": "info",
    "related_but_distinct": "secondary",
    "confirmed_distinct": "secondary",
    "unresolved": "secondary",
}


def _manual_value(raw: str):
    text = raw.strip()
    if not text:
        raise ValueError("observation value must not be blank")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@router.get("/entities", response_class=HTMLResponse)
def entities_page(
    request: Request, q: Annotated[str | None, Query(max_length=200)] = None
):
    engine = request.app.state.engine
    with session_scope(engine) as session:
        organisations = entities.list_organisations(session, query=q)
        counts = entities.organisation_counts(session)
        queue_size = len(list_queue(session))
        return _render(
            request,
            "entities.html",
            active="entities",
            organisations=organisations,
            counts=counts,
            queue_size=queue_size,
            q=q or "",
            organisation_types=[t.value for t in OrganisationType],
            identifier_schemes=[s.value for s in IdentifierScheme],
        )


@router.post("/entities/create")
def create_entity(
    request: Request,
    canonical_name: Annotated[str, Form()],
    legal_name: Annotated[str, Form()] = "",
    organisation_type: Annotated[str, Form()] = "unknown",
    identifier_scheme: Annotated[str, Form()] = "",
    identifier_value: Annotated[str, Form()] = "",
    domain: Annotated[str, Form()] = "",
):
    engine = request.app.state.engine
    if organisation_type not in {t.value for t in OrganisationType}:
        return _redirect("/entities", "Invalid organisation type", "danger")
    if identifier_value.strip() and identifier_scheme not in {
        s.value for s in IdentifierScheme
    }:
        return _redirect("/entities", "Invalid identifier scheme", "danger")
    identifiers = (
        [(identifier_scheme, identifier_value)] if identifier_value.strip() else None
    )
    try:
        with session_scope(engine) as session:
            organisation = entities.create_organisation(
                session,
                canonical_name,
                legal_name=legal_name or None,
                organisation_type=organisation_type,
                identifiers=identifiers,
                domains=[domain] if domain.strip() else None,
            )
            name = organisation.canonical_name
    except ValueError as exc:
        return _redirect("/entities", str(exc), "danger")
    return _redirect("/entities", f"Organisation “{name}” created")


@router.post("/entities/scan")
def run_match_scan(request: Request):
    engine = request.app.state.engine
    with session_scope(engine) as session:
        summary = scan_for_duplicates(session)
    block_note = ""
    if summary["oversized_blocks_skipped"]:
        block_note = (
            f"; {summary['oversized_blocks_skipped']} oversized generic-name blocks "
            "safely skipped"
        )
    return _redirect(
        "/resolution",
        f"Scan finished: {summary['pairs_scored']} pairs scored, "
        f"{summary['candidates_created']} new, {summary['candidates_updated']} refreshed"
        f"{block_note}",
    )


@router.get("/entities/{organisation_id}", response_class=HTMLResponse)
def entity_detail(request: Request, organisation_id: str):
    engine = request.app.state.engine
    with session_scope(engine) as session:
        organisation = entities.get_organisation(session, organisation_id)
        if organisation is None:
            return _redirect("/entities", "Organisation not found", "danger")
        intel = intelligence_profile.assemble(session, organisation_id)
        profile = intel["identity"]
        merges = list(
            session.execute(
                select(EntityMerge)
                .where(
                    (EntityMerge.survivor_id == profile["canonical"].id)
                    | (EntityMerge.absorbed_id == organisation_id)
                )
                .order_by(EntityMerge.performed_at.desc())
            ).scalars()
        )
        group_ids = [o.id for o in profile["group"]]
        edges = knowledge_graph.edges_for(session, organisation_id, include_historical=True)
        neighbours = knowledge_graph.neighbourhood(
            session, organisation_id, depth=2, limit=15
        )
        edge_orgs = {
            o.id: o
            for o in (
                session.get(type(organisation), edge.other_id) for edge in edges
            )
            if o is not None
        }
        return _render(
            request,
            "entity_detail.html",
            active="entities",
            organisation=organisation,
            profile=profile,
            intel=intel,
            merges=merges,
            edges=edges,
            edge_orgs=edge_orgs,
            neighbours=neighbours,
            participations=participations_for_organisation(session, group_ids),
            leads=leads_for_organisation(session, profile["canonical"].id),
            relationship_types=RELATIONSHIP_TYPES,
            other_organisations=[
                o
                for o in entities.list_organisations(session, limit=200)
                if o.id not in group_ids
            ],
        )


@router.post("/research-questions/{question_id}/{action}")
def research_question_action(request: Request, question_id: str, action: str):
    engine = request.app.state.engine
    if action not in ("resolved", "dismissed"):
        return _redirect("/entities", "Unknown research-question action", "danger")
    try:
        with session_scope(engine) as session:
            question = gaps.resolve_question(
                session, question_id, status=action, actor="user"
            )
            entity_id = question.entity_id
    except (LookupError, ValueError) as exc:
        return _redirect("/entities", str(exc), "danger")
    target = f"/entities/{entity_id}" if entity_id else "/entities"
    return _redirect(target, f"Research question {action}")


@router.post("/entities/{organisation_id}/observations/create")
def create_manual_observation(
    request: Request,
    organisation_id: str,
    source_document_id: Annotated[str, Form()],
    predicate: Annotated[str, Form()],
    value: Annotated[str, Form()],
    extraction_confidence: Annotated[float, Form()] = 0.8,
    human_verified: Annotated[str | None, Form()] = None,
):
    try:
        with session_scope(request.app.state.engine) as session:
            organisation = entities.get_organisation(session, organisation_id)
            if organisation is None:
                raise LookupError("organisation not found")
            document = session.get(SourceDocument, source_document_id.strip())
            if document is None:
                raise LookupError("source document not found")
            record_observation(
                session,
                document,
                predicate,
                _manual_value(value),
                subject_entity_id=organisation.id,
                extraction_method=ExtractionMethod.MANUAL,
                extraction_confidence=extraction_confidence,
                human_verified=human_verified == "1",
                verified_by="user" if human_verified == "1" else None,
            )
            intelligence_profile.refresh(session, organisation.id)
    except (LookupError, ValueError) as exc:
        return _redirect(f"/entities/{organisation_id}", str(exc), "danger")
    return _redirect(
        f"/entities/{organisation_id}",
        "Manual observation recorded with evidence provenance",
    )


@router.post("/entities/{organisation_id}/merge")
def merge_into_entity(
    request: Request,
    organisation_id: str,
    absorbed_id: Annotated[str, Form()],
    rationale: Annotated[str, Form()],
):
    engine = request.app.state.engine
    try:
        with session_scope(engine) as session:
            perform_merge(
                session,
                organisation_id,
                absorbed_id.strip(),
                rationale=rationale,
                performed_by="user",
            )
    except (ResolutionError, LookupError) as exc:
        return _redirect(f"/entities/{organisation_id}", str(exc), "danger")
    return _redirect(f"/entities/{organisation_id}", "Merged — original record preserved")


@router.post("/merges/{merge_id}/reverse")
def reverse_merge_action(
    request: Request, merge_id: str, reason: Annotated[str, Form()]
):
    engine = request.app.state.engine
    try:
        with session_scope(engine) as session:
            merge = reverse_merge(session, merge_id, reason=reason, performed_by="user")
            survivor_id = merge.survivor_id
    except (ResolutionError, LookupError) as exc:
        return _redirect("/entities", str(exc), "danger")
    return _redirect(f"/entities/{survivor_id}", "Merge reversed — records split")


@router.get("/resolution", response_class=HTMLResponse)
def resolution_page(request: Request, all: Annotated[str | None, Query()] = None):
    engine = request.app.state.engine
    show_all = all == "1"
    with session_scope(engine) as session:
        candidates = list_queue(session, include_resolved=show_all)
        rows = [
            {
                "candidate": candidate,
                "org_a": candidate.organisation_a,
                "org_b": candidate.organisation_b,
            }
            for candidate in candidates
        ]
        return _render(
            request,
            "resolution.html",
            active="resolution",
            rows=rows,
            show_all=show_all,
            match_state_badges=MATCH_STATE_BADGES,
        )


@router.post("/resolution/{candidate_id}/decide")
def decide_candidate(
    request: Request,
    candidate_id: str,
    decision: Annotated[str, Form()],
    survivor_id: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    engine = request.app.state.engine
    try:
        with session_scope(engine) as session:
            record_decision(
                session,
                candidate_id,
                decision,
                resolved_by="user",
                notes=notes.strip() or None,
                survivor_id=survivor_id.strip() or None,
            )
    except (ResolutionError, LookupError) as exc:
        return _redirect("/resolution", str(exc), "danger")
    return _redirect("/resolution", f"Recorded decision: {decision}")
