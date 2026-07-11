"""Industry-agnostic domain entities.

Deliberately near-empty at M0. Domain models (Organisation, Observation,
FactAssertion, ...) arrive at M4 per docs/roadmap.md — resist adding them earlier.
Dependency rule (ADR-0004): this package imports only stdlib + Pydantic.
"""

__version__ = "0.1.0"
