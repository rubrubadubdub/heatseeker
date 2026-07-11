"""Canonical source registry, contextual coverage, and evidence acquisition.

Lifecycle: proposed -> candidate -> active <-> degraded -> disabled / rejected.
Nothing is collected until robots and terms allow it (docs/architecture/source-discovery.md).
Coverage profiles preserve correlated industry/geography/facet applicability (ADR-0010).
"""

__version__ = "0.2.0"

COLLECTOR_VERSION = "collector/0.2"
