"""Custom Graphiti entity types (ADR-015).

Layer L5. Graphiti-specific — these are the framework's ontology, not our domain model.

Without a prescribed ontology Graphiti infers entity types freely, which produces a
graph whose categories drift from ours over time. ADR-015 already establishes that
Graphiti's internal consolidation is a retrieval optimisation rather than truth, but
the closer its categories track `EntityType`, the less its decisions diverge from
ours and the less work `entity_divergence` has to report.

Field sets are kept deliberately small. Every attribute here is something Graphiti
will ask the model to populate on each extraction, so a broad schema costs latency
and invites hallucinated detail. The authoritative attribute store is PostgreSQL —
these exist to help the graph organise itself, not to hold facts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from pca.domain.enums import EntityType


class Person(BaseModel):
    """A human being the user has mentioned."""

    role: str | None = Field(
        default=None,
        description="how this person relates to the user, e.g. sister, friend, colleague",
    )
    occupation: str | None = Field(
        default=None, description="what they do for a living, if stated"
    )


class Organization(BaseModel):
    """A company, employer, institution, or group."""

    kind: str | None = Field(
        default=None, description="e.g. employer, school, hospital, club"
    )


class Place(BaseModel):
    """A city, region, country, venue, or address."""

    kind: str | None = Field(
        default=None, description="e.g. city, state, country, venue, residence"
    )


class Project(BaseModel):
    """An ongoing effort, plan, or undertaking."""

    status: str | None = Field(
        default=None, description="e.g. planned, active, paused, finished"
    )


# Keys are the type names Graphiti uses in the graph. They intentionally match the
# names of our EntityType members so the two ontologies stay legible side by side.
GRAPHITI_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Organization": Organization,
    "Place": Place,
    "Project": Project,
}

# Mapping back to our own enum, for when a graph hit needs to be interpreted in
# domain terms. Anything unrecognised becomes OTHER rather than raising: Graphiti may
# legitimately produce a type we did not prescribe, and losing the hit entirely would
# be worse than labelling it loosely.
GRAPHITI_TYPE_TO_DOMAIN: dict[str, EntityType] = {
    "Person": EntityType.PERSON,
    "Organization": EntityType.ORGANIZATION,
    "Place": EntityType.PLACE,
    "Project": EntityType.PROJECT,
}


def to_domain_entity_type(graphiti_label: str | None) -> EntityType:
    if not graphiti_label:
        return EntityType.OTHER
    return GRAPHITI_TYPE_TO_DOMAIN.get(graphiti_label, EntityType.OTHER)
