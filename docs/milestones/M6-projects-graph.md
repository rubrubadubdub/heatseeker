# M6 — Projects, Relationships, Knowledge Graph

Spec: §13.9–§13.10 (Project/Participation), §13.12 (Relationship), §23 (graph
requirements + example queries), §35 M6.

## Deliverables (spec §35 M6)

Projects · participation · relationships · graph query layer · project workspace ·
relationship evidence.

**Acceptance:** companies connect via projects and business relationships · edge
confidence inspectable · historical relationships retain dates · useful multi-hop
queries possible.

## Design decisions

1. **Package**: new `packages/knowledge_graph` → `heatseeker_knowledge_graph`
   (planned in the architecture overview). Relational graph patterns only (§23.3
   explicitly defers a graph DB until justified).
2. **Edges are never deleted.** `end_relationship` stamps `valid_to` + status
   `historical`; a wrong edge becomes `retracted` — both keep their dates and evidence
   (acceptance #3; graph requirement "contradiction"/"manual confirmation").
3. **Merged organisations resolve to canonical** on both write and read: an edge added
   to an absorbed record lands on its survivor; traversal never splits a merge group
   into two nodes.
4. **Two edge sources, one traversal**: explicit `relationship` rows plus *derived*
   co-participation edges (two orgs on the same project). Derived edges carry the
   project id and min(participation confidence) — a real path explanation, not a
   stored duplicate that could drift.
5. **Types are extensible strings** with spec-suggested vocabularies as constants
   (relationship examples §13.12, participation roles §13.10) — packs may extend
   without core changes (§23.2 "industry-pack extensions").
6. **Multi-hop = bounded BFS** returning paths with per-edge confidence and the path
   product, so every hop stays inspectable. Depth ≤ 4, breadth-capped; enough for the
   §23.3 query shapes that current data can answer.
7. **Guidance integration**: the dashboard checklist gains a "map projects &
   relationships" step once a population exists (per the hand-holding preference).

## Qualification hardening (2026-07-18)

- Project, participation, and relationship writes validate status vocabularies, entity
  existence, value/date ranges, confidence bounds, and evidence references. Confidence
  above 0.5 and probable/confirmed participation require evidence that resolves to a
  stored source document or observation.
- Unconfirmed participation remains visible on the project workspace but is excluded
  from graph traversal. Historical/retracted records cannot be reopened or silently
  downgraded.
- Project co-participation edges are loaded in bounded set queries and aggregated by
  canonical peer/project, eliminating per-participation query fan-out and duplicate
  derived edges.
- Every create, strengthen, status change, end, and retract operation records an audit
  event; API and UI validation errors are surfaced rather than persisted.

## Schema (migration 0017)

- `project` — name, project_type_ids JSON (pack vocab), status
  (`planned|active|completed|cancelled|unknown`), location FK, geography_scope JSON,
  estimated_value + currency (nullable — no fabricated precision), actual + expected
  start/end dates, description, sector_ids JSON, evidence_ids JSON, created/updated.
- `project_participation` — project FK, organisation FK, role_type (string vocab),
  status (`unconfirmed|probable|confirmed|historical|retracted`), confidence,
  contract_value nullable, evidence_ids JSON, first/last_observed. Unique
  (project, organisation, role_type).
- `relationship` — subject/object organisation FKs (self-edge forbidden),
  relationship_type (string vocab), status (`active|historical|retracted`), confidence,
  valid_from/valid_to, evidence_ids JSON, created_by, created/updated. One *open* edge
  per (subject, object, type); ended edges accumulate as history.

## Module map (packages/knowledge_graph)

| Module | Contents |
|---|---|
| `models.py` | tables + vocab constants above |
| `projects.py` | create/list projects, add/update participation |
| `graph.py` | `add_relationship`, `end_relationship`, `retract_relationship`, `edges_for`, `neighbourhood`, `find_paths` |
| API/UI | `/projects` + `/projects/{id}` workspace; entity-page "Connections" section; `/api/projects*`, `/api/relationships*`, `/api/graph/*` |

## Acceptance → test map

| Acceptance | Test |
|---|---|
| companies connect via projects + relationships | participation and relationship edges both traversed; co-participation derived edge appears |
| edge confidence inspectable | every edge/path exposes confidence + evidence count; path product returned |
| historical relationships retain dates | end/retract keeps valid_from/valid_to + row; open-edge dedupe never touches history |
| multi-hop queries work | A —project— B —relationship— C found from A at depth 3 with confidence trail; min-confidence + depth filters |
