"""Tests for EntityService — the ADR-014 resolution policy.

The policy exists because the failure modes are wildly asymmetric:

    a duplicate entity   -> visible, annoying, fixable whenever
    a wrongly merged one -> invisible corruption of every future answer about
                            either person, and near-impossible to untangle later

So the rule is: link on a single confident match, create a *provisional duplicate*
on ambiguity, and never merge as a side effect of extraction.

The test that matters most is
`test_two_matching_entities_creates_a_provisional_instead_of_guessing`. If that ever
starts failing because someone "improved" resolution by picking the best score, the
system has quietly acquired its worst failure mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.enums import EntityType, ResolutionOutcome
from pca.domain.ids import EntityId
from pca.services.entities import EntityService
from tests.fakes.clock import FakeClock
from tests.fakes.memory_repositories import FakeEntityRepository

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def repository() -> FakeEntityRepository:
    return FakeEntityRepository()


@pytest.fixture
def service(repository: FakeEntityRepository) -> EntityService:
    return EntityService(repository=repository, clock=FakeClock(start=START))


async def seed(
    repository: FakeEntityRepository, name: str, count: int = 1
) -> list[EntityId]:
    """Insert `count` distinct entities all sharing the same name.

    Real UUIDs, not strings — comparing a UUID against a string would make the
    "did not pick either existing entity" assertions pass vacuously.
    """
    ids: list[EntityId] = []
    for _ in range(count):
        entity_id = EntityId(uuid4())
        await repository.create(
            entity_id=entity_id,
            name=name,
            entity_type=EntityType.PERSON,
            created_at=START,
        )
        ids.append(entity_id)
    return ids


# ------------------------------------------------------------------- creation


async def test_unknown_mention_creates_a_non_provisional_entity(
    service: EntityService,
) -> None:
    decision = await service.resolve_for_extraction("Priya", EntityType.PERSON)

    assert decision.outcome is ResolutionOutcome.CREATED
    assert decision.entity.name == "Priya"
    assert decision.entity.is_provisional is False
    assert decision.needs_clarification is False


async def test_entity_type_is_preserved(service: EntityService) -> None:
    decision = await service.resolve_for_extraction("Google", EntityType.ORGANIZATION)

    assert decision.entity.entity_type is EntityType.ORGANIZATION


async def test_empty_mention_is_rejected(service: EntityService) -> None:
    with pytest.raises(ValueError, match="empty mention"):
        await service.resolve_for_extraction("   ")


# -------------------------------------------------------------------- linking


async def test_single_existing_match_is_linked(service: EntityService) -> None:
    first = await service.resolve_for_extraction("Priya")

    second = await service.resolve_for_extraction("Priya")

    assert second.outcome is ResolutionOutcome.LINKED
    assert second.entity.id == first.entity.id


async def test_matching_is_case_insensitive(service: EntityService) -> None:
    first = await service.resolve_for_extraction("Priya")

    second = await service.resolve_for_extraction("priya")

    assert second.outcome is ResolutionOutcome.LINKED
    assert second.entity.id == first.entity.id


async def test_alias_match_links_to_the_same_entity(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    created = await service.resolve_for_extraction("Priya Sharma")
    await repository.add_aliases(created.entity.id, ["Priya"])

    decision = await service.resolve_for_extraction("Priya")

    assert decision.outcome is ResolutionOutcome.LINKED
    assert decision.entity.id == created.entity.id


# ----------------------------------------------------- the ADR-014 decision


async def test_two_matching_entities_creates_a_provisional_instead_of_guessing(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """**The central test of Unit 2.**

    Two people named Sarah already exist. A third mention of "Sarah" is genuinely
    ambiguous, and picking either one would silently attach the new fact to
    possibly the wrong person, permanently, with no signal that it happened.

    Creating a visible duplicate is the strictly safer failure direction.
    """
    existing = await seed(repository, "Sarah", count=2)

    decision = await service.resolve_for_extraction("Sarah")

    assert decision.outcome is ResolutionOutcome.PROVISIONAL
    assert decision.entity.is_provisional is True
    assert decision.needs_clarification is True
    # And crucially: it did not pick either existing entity.
    assert decision.entity.id not in set(existing)


async def test_ambiguous_resolution_reports_the_competing_candidates(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """The alternatives must be visible so a human can decide."""
    await seed(repository, "Sarah", count=2)

    decision = await service.resolve_for_extraction("Sarah")

    assert len(decision.considered) == 2


async def test_provisional_entities_are_listable(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """Without this, the duplicates ADR-014 deliberately creates would pile up
    unseen — turning a visible problem back into an invisible one."""
    await seed(repository, "Sarah", count=2)
    await service.resolve_for_extraction("Sarah")

    provisional = await service.list_provisional()

    assert len(provisional) == 1
    assert provisional[0].is_provisional


async def test_resolution_never_merges(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """Merging must never be a side effect of extraction."""
    await seed(repository, "Sarah", count=2)

    await service.resolve_for_extraction("Sarah")

    assert repository.merged == {}, "extraction must never merge entities"


# -------------------------------------------------------------------- batching


async def test_repeated_mentions_in_one_batch_create_one_entity(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """A message naming "Priya" three times must not create three entities.

    Asserted on distinct entity ids rather than on the size of the returned dict:
    the dict is a convenience, the entity count is the property that matters.
    """
    decisions = await service.resolve_many(["Priya", "Priya", "priya"])

    assert len({d.entity.id for d in decisions.values()}) == 1
    assert await repository.count() == 1


async def test_batch_resolves_distinct_names_separately(service: EntityService) -> None:
    decisions = await service.resolve_many(["Priya", "Suresh"])

    assert len(decisions) == 2
    assert {d.entity.name for d in decisions.values()} == {"Priya", "Suresh"}


# ------------------------------------------------------------ explicit merging


async def test_merge_carries_the_absorbed_name_across_as_an_alias(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """Otherwise future mentions of the old name would create yet another duplicate."""
    keep = await service.resolve_for_extraction("Priya Sharma")
    absorb = await service.resolve_for_extraction("Priya S")

    await service.merge(keep.entity.id, absorb.entity.id, reason="same person")

    assert "Priya S" in repository.aliases[keep.entity.id]


async def test_merge_records_rather_than_destroys(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """ADR-014 requires merges to stay reversible."""
    keep = await service.resolve_for_extraction("Priya Sharma")
    absorb = await service.resolve_for_extraction("Priya S")

    await service.merge(keep.entity.id, absorb.entity.id, reason="same person")

    assert absorb.entity.id in repository.merged
    target, reason, when = repository.merged[absorb.entity.id]
    assert target == keep.entity.id
    assert reason == "same person"
    assert when == START


async def test_absorbed_entity_stops_being_a_resolution_candidate(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    keep = await service.resolve_for_extraction("Priya Sharma")
    absorb = await service.resolve_for_extraction("Priya S")
    await service.merge(keep.entity.id, absorb.entity.id, reason="same person")

    decision = await service.resolve_for_extraction("Priya S")

    # Resolves via the alias to the surviving entity, not to the absorbed row.
    assert decision.outcome is ResolutionOutcome.LINKED
    assert decision.entity.id == keep.entity.id


async def test_self_merge_is_rejected(service: EntityService) -> None:
    created = await service.resolve_for_extraction("Priya")

    with pytest.raises(ValueError, match="into itself"):
        await service.merge(created.entity.id, created.entity.id, reason="nonsense")


async def test_merging_a_missing_entity_is_rejected(service: EntityService) -> None:
    created = await service.resolve_for_extraction("Priya")

    with pytest.raises(ValueError, match="must exist"):
        await service.merge(
            created.entity.id,
            EntityId(uuid4()),
            reason="no such entity",
        )


# ===========================================================================
# The self entity
#
# Found by a live test: a single message produced an entity literally named "user"
# with type "other". Extraction refers to the speaker inconsistently — "user", "me",
# "I", "myself" — and because those are *different names*, resolution takes the
# CREATED branch each time without even flagging ambiguity.
#
# That is worse than the ambiguous-duplicate case ADR-014 handles: there is no signal
# at all, and every relationship about the user silently fragments across variants.
# ===========================================================================


@pytest.mark.parametrize(
    "mention",
    ["me", "I", "i", "myself", "user", "the user", "My", "mine", "self"],
)
async def test_first_person_mentions_all_resolve_to_one_entity(
    service: EntityService, repository: FakeEntityRepository, mention: str
) -> None:
    first = await service.resolve_for_extraction("me")

    second = await service.resolve_for_extraction(mention)

    assert second.outcome is ResolutionOutcome.LINKED
    assert second.entity.id == first.entity.id
    assert await repository.count() == 1


async def test_every_self_variant_in_one_batch_yields_a_single_entity(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """The failure mode this prevents, exercised directly."""
    await service.resolve_many(["me", "I", "user", "myself", "the user"])

    assert await repository.count() == 1


async def test_self_entity_is_typed_as_a_person(service: EntityService) -> None:
    """Not OTHER, which is what the model produced live.

    Typing the user as OTHER would exclude them from person-scoped queries — a poor
    default for the one entity appearing in most relationships.
    """
    decision = await service.resolve_for_extraction("me")

    assert decision.entity.entity_type is EntityType.PERSON
    assert decision.entity.is_provisional is False


async def test_self_entity_uses_a_canonical_name(service: EntityService) -> None:
    decision = await service.resolve_for_extraction("I")

    assert decision.entity.name == "the user"


async def test_self_entity_carries_every_alias(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """Seeded up front so a later lookup matches by alias even if the fast path is
    bypassed."""
    decision = await service.resolve_for_extraction("me")

    aliases = repository.aliases[decision.entity.id]
    assert {"me", "i", "user", "myself"} <= aliases


async def test_an_existing_self_entity_is_adopted_rather_than_duplicated(
    service: EntityService, repository: FakeEntityRepository
) -> None:
    """Handles data already written before canonicalisation existed.

    A prior run created an entity literally named "user". A fresh mention must adopt
    it rather than create a second self entity beside it.
    """
    legacy_id = EntityId(uuid4())
    await repository.create(
        entity_id=legacy_id,
        name="user",
        entity_type=EntityType.OTHER,
        created_at=START,
    )

    decision = await service.resolve_for_extraction("me")

    assert decision.entity.id == legacy_id
    assert await repository.count() == 1
    # And it gains the canonical alias set so future lookups converge.
    assert "the user" in repository.aliases[legacy_id]


async def test_a_real_person_is_not_confused_with_the_self_entity(
    service: EntityService,
) -> None:
    self_decision = await service.resolve_for_extraction("me")
    pradeep = await service.resolve_for_extraction("Pradeep")

    assert pradeep.entity.id != self_decision.entity.id
    assert pradeep.entity.name == "Pradeep"


async def test_self_and_a_colleague_can_form_a_relationship(
    service: EntityService,
) -> None:
    """The concrete case from the live test: "My Colleague Pradeep".

    Both endpoints must resolve to distinct entities, or the relationship is dropped.
    """
    from pca.domain.ids import MemoryId
    from pca.domain.memory import ProvenanceRef, Relationship
    from pca.domain.enums import Origin
    from pca.domain.ids import EpisodeId

    me = await service.resolve_for_extraction("me")
    colleague = await service.resolve_for_extraction("Pradeep")

    relationship = Relationship(
        id=MemoryId(uuid4()),
        from_entity_id=me.entity.id,
        to_entity_id=colleague.entity.id,
        relation_type="colleague",
        origin=Origin.USER_STATED,
        provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
    )

    assert relationship.from_entity_id != relationship.to_entity_id
