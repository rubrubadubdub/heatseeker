"""GUI routes for bulk-dataset discovery imports (M5, spec §9.2, §12.2)."""

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from heatseeker_common.db import session_scope
from heatseeker_industry_packs.loader import discover_packs
from heatseeker_intelligence.discovery import MappingSpec, create_import_run, list_runs
from heatseeker_source_registry.scopes import active_scope

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)

RUN_STATUS_BADGES = {
    "queued": "secondary",
    "running": "primary",
    "succeeded": "success",
    "failed": "danger",
}

_COLUMN_FIELDS = (
    ("name", "Company name column", True),
    ("legal_name", "Legal name column", False),
    ("identifier", "Identifier column (e.g. ABN)", False),
    ("registration_status", "Registration status column", False),
    ("domain", "Website column", False),
    ("locality", "Town/locality column", False),
    ("region", "State/region column", False),
    ("postcode", "Postcode column", False),
    ("street_address", "Street address column", False),
    ("employees_band", "Employee band column", False),
    ("email", "Public business email column", False),
    ("phone", "Public business phone column", False),
    ("instagram", "Instagram profile column", False),
    ("facebook", "Facebook business-page column", False),
    ("linkedin", "LinkedIn organisation-page column", False),
    ("youtube", "YouTube channel column", False),
    ("tiktok", "TikTok business-profile column", False),
    ("x_profile", "X/Twitter profile column", False),
    ("social_profile", "Other supported social-profile column", False),
    ("source_record_url", "Any source/listing record URL column", False),
    ("source_label", "Source/listing label column", False),
    ("service_claim", "Service ID column (optional, pack vocabulary)", False),
    ("archetype_claim", "Archetype ID column (optional, pack vocabulary)", False),
)


@router.get("/discovery", response_class=HTMLResponse)
def discovery_page(request: Request):
    with session_scope(request.app.state.engine) as session:
        runs = list_runs(session)
        scope = active_scope(session)
        return _render(
            request,
            "discovery.html",
            active="discovery",
            runs=runs,
            scope=scope,
            column_fields=_COLUMN_FIELDS,
            pack_ids=[path.name for path in discover_packs()],
            run_status_badges=RUN_STATUS_BADGES,
        )


@router.post("/discovery/import")
async def discovery_import(
    request: Request,
    dataset_file: Annotated[UploadFile, File()],
    dataset_name: Annotated[str, Form()],
    publisher: Annotated[str, Form()] = "",
    dataset_version: Annotated[str, Form()] = "",
    coverage_date: Annotated[str, Form()] = "",
    licence_note: Annotated[str, Form()] = "",
    authority_tier: Annotated[int, Form()] = 5,
    pack_id: Annotated[str, Form()] = "",
    identifier_scheme: Annotated[str, Form()] = "abn",
    country: Annotated[str, Form()] = "",
    col_name: Annotated[str, Form()] = "",
    col_legal_name: Annotated[str, Form()] = "",
    col_identifier: Annotated[str, Form()] = "",
    col_registration_status: Annotated[str, Form()] = "",
    col_domain: Annotated[str, Form()] = "",
    col_locality: Annotated[str, Form()] = "",
    col_region: Annotated[str, Form()] = "",
    col_postcode: Annotated[str, Form()] = "",
    col_street_address: Annotated[str, Form()] = "",
    col_employees_band: Annotated[str, Form()] = "",
    col_email: Annotated[str, Form()] = "",
    col_phone: Annotated[str, Form()] = "",
    col_instagram: Annotated[str, Form()] = "",
    col_facebook: Annotated[str, Form()] = "",
    col_linkedin: Annotated[str, Form()] = "",
    col_youtube: Annotated[str, Form()] = "",
    col_tiktok: Annotated[str, Form()] = "",
    col_x_profile: Annotated[str, Form()] = "",
    col_social_profile: Annotated[str, Form()] = "",
    col_source_record_url: Annotated[str, Form()] = "",
    col_source_label: Annotated[str, Form()] = "",
    col_service_claim: Annotated[str, Form()] = "",
    col_archetype_claim: Annotated[str, Form()] = "",
):
    engine = request.app.state.engine
    settings = request.app.state.settings
    upload_limit = settings.evidence_upload_max_bytes
    content = await dataset_file.read(upload_limit + 1)
    if len(content) > upload_limit:
        return _redirect(
            "/discovery", f"Dataset exceeds {upload_limit:,} compressed bytes", "danger"
        )

    columns = {
        field_name: value.strip()
        for field_name, value in (
            ("name", col_name),
            ("legal_name", col_legal_name),
            ("identifier", col_identifier),
            ("registration_status", col_registration_status),
            ("domain", col_domain),
            ("locality", col_locality),
            ("region", col_region),
            ("postcode", col_postcode),
            ("street_address", col_street_address),
            ("employees_band", col_employees_band),
            ("email", col_email),
            ("phone", col_phone),
            ("instagram", col_instagram),
            ("facebook", col_facebook),
            ("linkedin", col_linkedin),
            ("youtube", col_youtube),
            ("tiktok", col_tiktok),
            ("x_profile", col_x_profile),
            ("social_profile", col_social_profile),
            ("source_record_url", col_source_record_url),
            ("source_label", col_source_label),
            ("service_claim", col_service_claim),
            ("archetype_claim", col_archetype_claim),
        )
        if value.strip()
    }
    constants = {}
    if columns.get("identifier") and identifier_scheme.strip():
        constants["identifier_scheme"] = identifier_scheme.strip().lower()
    if country.strip():
        constants["country"] = country.strip().upper()
    if pack_id.strip():
        constants["pack_id"] = pack_id.strip()

    try:
        mapping = MappingSpec(columns=columns, constants=constants)
        with session_scope(engine) as session:
            run = create_import_run(
                session,
                settings,
                content,
                dataset_name=dataset_name,
                mapping=mapping,
                filename=dataset_file.filename or "dataset.csv",
                publisher=publisher,
                dataset_version=dataset_version,
                coverage_date=coverage_date,
                licence_note=licence_note,
                actor="user",
                authority_tier=authority_tier,
            )
            dataset = run.dataset_name
    except ValueError as exc:
        return _redirect("/discovery", str(exc), "danger")
    return _redirect(
        "/discovery",
        f"Import of “{dataset}” queued — rows become evidence-backed organisations; "
        "duplicates will land in the resolution queue",
    )
