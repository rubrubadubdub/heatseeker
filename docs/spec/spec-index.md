# Spec Index — Targeted Reading Map

Line-number map for the two master specs. Use `Read` with `offset`/`limit` to load only
the section you need. **Specs are frozen; these line numbers are stable.**

## Spec 1: Phase 1 platform spec (authoritative)

File: `docs/Phase 1 Initial Instructions - niche_industry_intelligence_phase_1_dev_spec.md`
(3,655 lines)

| § | Section | Lines | Read when… |
|---|---|---|---|
| 1 | Executive Summary | 13–50 | rarely — digest covers it |
| 2 | Product Statement | 52–56 | rarely |
| 3 | Problem Definition (info/classification/lead/market problems) | 58–193 | understanding "why" behind a requirement |
| 4 | Phase 1 Objectives (primary 17 + secondary) | 195–232 | scoping any milestone |
| 5 | Non-Goals | 234–253 | scope questions |
| 6 | Guiding Principles (6.1 evidence, 6.3 missing≠false, 6.8 AI bounds…) | 256–374 | design decisions |
| 7 | Product Architecture (mermaid flow, layers A–E) | 376–441 | architecture work |
| 8 | Industry-Pack System (contents 8.2, taxonomies 8.3, scaffolding pack 8.4) | 444–591 | M1 pack framework |
| 9 | Research Modes (discovery/deep/market/event/lead) | 593–721 | research orchestration, UI |
| 10 | Source Strategy (categories 10.1, registry schema 10.2, hierarchy, independence) | 723–863 | M2 source registry |
| 11 | Responsible Crawling (robots 11.3, terms 11.4, politeness 11.5, frontier 11.6, change detection 11.8) | 865–979 | M3 crawler |
| 12 | Data Ingestion Beyond Crawling (APIs, bulk, PDF, manual) | 981–1017 | M2, importers |
| 13 | **Core Data Model** (all 17 entity YAML schemas) | 1019–1385 | schema design — see breakdown below |
| 14 | Entity Resolution (signals, states, no-destructive-merge, queue) | 1387–1436 | M4 |
| 15 | Classification Framework (multi-axis 15.1, method, explainability, learning) | 1438–1548 | M5 |
| 16 | Company Size & Operating Tier (indicators, tier rubric) | 1550–1631 | M5 |
| 17 | Accuracy & Confidence System (formula 17.2, authority, freshness, corroboration, contradiction) | 1633–1721 | confidence engine |
| 18 | Company Profile (UI sections) | 1723–1835 | M5 profile UI |
| 19 | Lead Intelligence (dimensions, need/negative/timing signals, explanations) | 1837–1952 | M8 |
| 20 | Contact & Role Intelligence (priority order, role taxonomy, verification) | 1954–1986 | contacts |
| 21 | Macro & Market Intelligence (PESTLE domains, MarketSignal, macro→micro, indices) | 1988–2133 | M7, M9 |
| 22 | News/Events/Catalyst Engine (story clustering, event taxonomy, EventImpact) | 2135–2216 | M7 |
| 23 | Relationship & Knowledge Graph (requirements, example queries) | 2218–2256 | M6 |
| 24 | Research Orchestration (plans, stages, adaptive research, budgets) | 2258–2315 | research queues |
| 25 | Programmatic Action Layer (rule engine, rule example, exports/API) | 2317–2379 | M10 |
| 26 | AI Architecture & Boundaries (roles, code-owned tasks, output reqs, cost control) | 2382–2450 | M11, any AI use |
| 27 | Search & Discovery Engine | 2452–2483 | search features |
| 28 | User Interface (nav, dashboard, explorer, lead queue, evidence viewer, pack editor) | 2485–2616 | frontend work |
| 29 | Local Technical Architecture (repo shape 29.1, stack 29.2, deployment, storage, install) | 2618–2717 | M0, stack decisions |
| 30 | Workflow & Job System (job classes, properties, priority, failure) | 2719–2762 | M0 job framework |
| 31 | Data Quality & Governance (dimensions, dashboard, audit, versioning) | 2764–2805 | quality features |
| 32 | Security & Privacy (local defaults, PII rules, suppression) | 2807–2849 | any PII/contact/export work |
| 33 | Analytics & Reporting | 2851–2915 | M9, reports |
| 34 | Scaffolding Initial Research Questions | 2917–2966 | M1 pack content |
| 35 | **Development Milestones M0–M12** (deliverables + acceptance) | 2968–3225 | distilled in roadmap.md |
| 36 | MVP Definition (19 points) | 3227–3253 | scope checks |
| 37 | Testing Strategy (unit, fixtures 37.2, e2e, accuracy eval, crawler tests) | 3255–3337 | test design |
| 38 | Observability (metrics, diagnostic views) | 3339–3376 | M0 logging, dashboards |
| 39 | Failure Modes & Mitigations (11 named failure modes) | 3378–3529 | design reviews |
| 40 | Development-Agent Operating Guidance | 3531–3554 | rarely — digest covers it |
| 41 | Definition of Done (20 points) | 3556–3581 | phase completion checks |
| 42 | Final Product Philosophy | 3583–3610 | rarely |
| 43 | External Standards & Reference APIs (ABR, ASIC, NZBN, Companies House, GDELT…) | 3612–3655 | source/collector work |

### §13 Core Data Model breakdown (entity → lines)

| Entity | Lines | Entity | Lines |
|---|---|---|---|
| Organisation | 1023–1046 | Relationship | 1258–1273 |
| OperationalUnit | 1048–1064 | SourceDocument | 1275–1294 |
| Location | 1066–1082 | Observation | 1296–1314 |
| Person / RoleAssignment | 1084–1112 | FactAssertion | 1316–1345 |
| ContactPoint | 1114–1136 | ResearchQuestion | 1347–1362 |
| ClassificationAssignment | 1138–1162 | AccountOpportunity | 1364–1383 |
| Capability / CapabilityAssignment | 1164–1194 | Event | 1236–1256 |
| Product/System/Equipment | 1196–1198 | ProjectParticipation | 1221–1234 |
| Project | 1200–1219 | | |

## Spec 2: Local Market Catalyst Intelligence (future phase — NOT Phase 1 scope)

File: `docs/local_market_catalyst_intelligence_dev_spec.md` (2,709 lines)

A separate, later-phase system: news-led, movement-confirmed equity research with
adversarial evidence review. Relevance to Phase 1: the evidence/story/event/source-lineage
architecture must remain compatible (Phase 1 spec §1 header, §41.20). Consult only when a
Phase 1 design choice might foreclose it.

Key sections: 1 Executive Summary (11–56) · 4 Design Principles (137–247) · 5 High-Level
Architecture (249–279) · 7 Core Domain Model — Security/Company/Source/RawDocument/
ParsedArticle/Story/Claim/ImpactEdge/Snapshot/Anomaly/Candidate/Evidence/Dossier/Outcome
(353–685) · 8 Pipeline Overview (687–710) · Stages 1–7 (712–1201+).

Shared concepts with Phase 1 (keep models compatible): raw document preservation, story
clustering, syndication lineage / source independence, claim extraction, impact graph,
candidate expiry, outcome tracking.
