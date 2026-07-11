# ADR-0009: Server-rendered GUI — FastAPI + Jinja2 + Bootstrap 5 + htmx

**Status:** Accepted · **Date:** 2026-07-11 · **Adjusts:** ADR-0002 (React noted for later
milestones), overview.md repo layout (`applications/frontend` dropped)

## Context

The owner wants a browser GUI now (M0/M1 surfaces) that is robust, intuitive,
expandable, and above all straightforward and maintainable. The roadmap had assumed a
React + Vite SPA at M5. A SPA brings a Node toolchain, a build step, dependency churn,
and a second language surface — heavy for a single-user local tool maintained largely
by AI agents, and it breaks the "one command, works offline" install goal (spec §29.5).

## Decision

- **Server-rendered pages** inside the existing FastAPI app: Jinja2 templates +
  **Bootstrap 5** (vendored, no CDN — offline-capable) + **htmx** (vendored) for
  partial updates (auto-refreshing job tables, form posts without full reloads).
- UI routes live at `/` (dashboard), `/jobs`, `/packs`, `/backups`, `/health-ui`;
  the JSON API moves under **`/api/*`** (`/api/health`, `/api/jobs`, …) with `/health`
  kept as an ops alias.
- Templates/static ship inside `heatseeker_api` (`templates/`, `static/vendor/`);
  no build step, no Node, no bundler. UI pages call the same Python functions as the
  API — never HTTP round-trips to itself.
- Expansion pattern: each future milestone adds a template + route (companies explorer,
  evidence viewer, queues — spec §28); htmx covers interactivity until something
  genuinely needs client state.

## Consequences

One language, one process, zero build tooling; any agent can extend the UI by editing a
template. If a later workspace (e.g. the graph visualiser, spec §23.2) outgrows
htmx, an island of richer JS (or a revisit of React) can be introduced *for that page
only* via a superseding ADR. Vendored assets are pinned files in git — upgrades are
deliberate single-file swaps.

## Alternatives rejected

- **React + Vite SPA now**: build step + Node dependency + API/client duplication;
  contradicts "straightforward and maintainable" for this deployment shape.
- **CDN-loaded Bootstrap/htmx**: breaks offline/local-first defaults (spec §32.1).
- **Streamlit/NiceGUI**: fast to start, but opinionated runtimes that fight the
  existing FastAPI app and get awkward at "evidence viewer" complexity.
