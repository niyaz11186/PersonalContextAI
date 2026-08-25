"""EntityService — resolution, creation, and explicit merging (ADR-014).

Layer L3.

This is the most consequential service in Unit 2, because its failure mode is
asymmetric and the asymmetry is severe:

    A duplicate entity  -> visible, annoying, correctable at any time.
    A wrongly merged one -> invisible corruption. Every future answer about either
                            person is contaminated, and untangling it after months
                            of accumulated facts is close to impossible.

Extraction is fully automatic (FR-02.3), so there is no human in the loop when the
system meets "Sarah" and already knows three. ADR-014 therefore forbids resolving
ambiguity by picking the best score:

    high confidence -> link
    ambiguous       -> create a NEW provisional entity and flag it
    no match        -> create

Merging is always an explicit, recorded, reversible operation. It is never a side
effect of extraction.

Resolution is by name and alias rather than embedding similarity, deliberately.
Similarity gives a comforting number that invites exactly the silent merging this
design exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from pca.domain.enums import EntityType, ResolutionOutcome
from pca.domain.ids import EntityId
from pca.domain.memory import Entity, EntityMatch, ResolutionDecision
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import EntityRepositoryPort

_log = get_logger(__name__)

# Match scores. Exact-case is treated as strongest, then case-insensitive, then an
# alias hit. These are coarse on purpose: the decision that matters is "one
# candidate or several", not a fine ranking between them.
SCORE_EXACT = 1.0
SCORE_CASE_INSENSITIVE = 0.95
SCORE_ALIAS = 0.90

LINK_THRESHOLD = 0.85

# --------------------------------------------------------------------------
# The self entity.
#
# The user is the single most important entity in a personal-context system —
# almost every relationship radiates from them. But extraction refers to them
# inconsistently: "user" on one message, "me" on another, "I" on a third.
#
# Without canonicalisation each variant is a *different name*, so resolution takes
# the CREATED branch every time and never even flags ambiguity. The result is a
# silent fan-out of self-duplicates, and every relationship about the user is split
# across them. That is worse than the ambiguous-duplicate case ADR-014 addresses,
# because there is no signal at all.
#
# Observed live: a single message produced an entity literally named "user" with
# type "other".
# --------------------------------------------------------------------------
SELF_ENTITY_NAME = "the user"

SELF_ALIASES: frozenset[str] = frozenset(
    {
        "the user",
        "user",
        "me",
        "i",
        "myself",
        "my",
        "mine",
        "self",
        "the speaker",
        "narrator",
    }
)


def is_self_mention(mention: str) -> bool:
    return mention.strip().casefold() in SELF_ALIASES


class EntityService:
    def __init__(self, repository: EntityRepositoryPort, clock: ClockPort) -> None:
        self._repository = repository
        self._clock = clock

    # ----------------------------------------------------------------- resolve

    async def resolve_for_extraction(
        self, mention: str, entity_type: EntityType = EntityType.PERSON
    ) -> ResolutionDecision:
        """Resolve a mention during automatic extraction.

        Never merges. Never picks between competing candidates.
        """
        mention = mention.strip()
        if not mention:
            raise ValueError("cannot resolve an empty mention")

        # First-person mentions collapse onto one canonical entity before any name
        # matching happens. Otherwise "me" and "user" are simply different names and
        # each creates its own entity, fragmenting every relationship about the user.
        if is_self_mention(mention):
            entity = await self.resolve_self()
            _log.info("self_mention_linked", mention=mention, entity_id=str(entity.id))
            return ResolutionDecision(
                outcome=ResolutionOutcome.LINKED,
                entity=entity,
                considered=[EntityMatch(entity=entity, score=SCORE_EXACT)],
            )

        candidates = await self._score_candidates(mention)
        strong = [c for c in candidates if c.score >= LINK_THRESHOLD]

        if len(strong) == 1:
            entity = strong[0].entity
            _log.info(
                "entity_linked",
                mention=mention,
                entity_id=str(entity.id),
                score=strong[0].score,
            )
            return ResolutionDecision(
                outcome=ResolutionOutcome.LINKED,
                entity=entity,
                considered=candidates,
            )

        if len(strong) > 1:
            # The ADR-014 decision point. Creating a third "Sarah" looks wasteful
            # until you consider the alternative: silently attributing a fact to the
            # wrong person, permanently, with no signal that it happened.
            provisional = await self._create(
                mention, entity_type, is_provisional=True
            )
            _log.warning(
                "entity_ambiguous_provisional_created",
                mention=mention,
                entity_id=str(provisional.id),
                competing=[str(c.entity.id) for c in strong],
                reason="multiple existing entities matched; ADR-014 forbids silent selection",
            )
            return ResolutionDecision(
                outcome=ResolutionOutcome.PROVISIONAL,
                entity=provisional,
                considered=candidates,
                needs_clarification=True,
            )

        created = await self._create(mention, entity_type, is_provisional=False)
        _log.info("entity_created", mention=mention, entity_id=str(created.id))
        return ResolutionDecision(
            outcome=ResolutionOutcome.CREATED,
            entity=created,
            considered=candidates,
        )

    async def resolve_self(self) -> Entity:
        """Find or create the canonical entity representing the user.

        Seeded with every first-person alias so that a later mention of any of them
        matches by alias even if this method is bypassed.

        Typed PERSON, not OTHER. The user is a person, and typing them otherwise
        would exclude them from person-scoped queries — which, for the one entity
        that appears in most relationships, is a bad default.
        """
        existing = await self._repository.find_by_name(SELF_ENTITY_NAME)
        if existing:
            return existing[0]

        # A prior mention may have created the self entity under a different alias.
        for alias in sorted(SELF_ALIASES):
            found = await self._repository.find_by_name(alias)
            if found:
                # Adopt it: give it the canonical name's alias set so subsequent
                # lookups converge rather than continuing to diverge.
                await self._repository.add_aliases(
                    found[0].id, sorted(SELF_ALIASES)
                )
                _log.info(
                    "self_entity_adopted_existing",
                    entity_id=str(found[0].id),
                    matched_alias=alias,
                )
                return found[0]

        created = await self._repository.create(
            entity_id=EntityId(uuid4()),
            name=SELF_ENTITY_NAME,
            entity_type=EntityType.PERSON,
            created_at=self._clock.now(),
            is_provisional=False,
            aliases=sorted(SELF_ALIASES),
        )
        _log.info("self_entity_created", entity_id=str(created.id))
        return created

    async def resolve_many(
        self, mentions: Sequence[str], entity_type: EntityType = EntityType.PERSON
    ) -> dict[str, ResolutionDecision]:
        """Resolve several mentions, deduplicating within the batch.

        Deduplication matters: a single message mentioning "Priya" three times must
        not create three entities.
        """
        decisions: dict[str, ResolutionDecision] = {}
        seen: set[str] = set()
        for mention in mentions:
            key = mention.strip()
            # Case-insensitive, matching how resolution itself matches. A
            # case-sensitive key would send "Priya" and "priya" through two
            # lookups, and left MemoryService and this method disagreeing about
            # what counts as the same mention.
            if not key or key.casefold() in seen:
                continue
            seen.add(key.casefold())
            decisions[key] = await self.resolve_for_extraction(key, entity_type)
        return decisions

    # ------------------------------------------------------------------- reads

    async def get(self, entity_id: EntityId) -> Entity | None:
        return await self._repository.get(entity_id)

    async def find(self, name: str) -> Sequence[Entity]:
        return await self._repository.find_by_name(name)

    async def list_provisional(self, limit: int = 100) -> Sequence[Entity]:
        """Ambiguous entities awaiting a deliberate decision.

        Without this the duplicates ADR-014 deliberately creates would accumulate
        unseen, turning a visible problem back into an invisible one.
        """
        return await self._repository.list_provisional(limit)

    # ------------------------------------------------------------------ merges

    async def merge(self, keep: EntityId, absorb: EntityId, reason: str) -> None:
        """Explicitly merge two entities.

        Only ever called deliberately — by a user action or an operator. Records
        rather than destroys, so it remains reversible.
        """
        if keep == absorb:
            raise ValueError("cannot merge an entity into itself")

        target = await self._repository.get(keep)
        source = await self._repository.get(absorb)
        if target is None or source is None:
            raise ValueError("both entities must exist to merge")

        # Carry the absorbed name across as an alias, so future mentions of it
        # resolve to the surviving entity rather than creating yet another duplicate.
        aliases = {source.name, *source.aliases} - {target.name}
        if aliases:
            await self._repository.add_aliases(keep, sorted(aliases))

        await self._repository.merge(
            keep=keep, absorb=absorb, reason=reason, merged_at=self._clock.now()
        )
        _log.info(
            "entities_merged",
            keep=str(keep),
            absorbed=str(absorb),
            aliases_carried=sorted(aliases),
            reason=reason,
        )

    # --------------------------------------------------------------- internals

    async def _score_candidates(self, mention: str) -> list[EntityMatch]:
        found = await self._repository.find_by_name(mention)
        lowered = mention.casefold()

        matches: list[EntityMatch] = []
        for entity in found:
            if entity.name == mention:
                score = SCORE_EXACT
            elif entity.name.casefold() == lowered:
                score = SCORE_CASE_INSENSITIVE
            elif any(alias.casefold() == lowered for alias in entity.aliases):
                score = SCORE_ALIAS
            else:
                continue
            matches.append(EntityMatch(entity=entity, score=score))

        return sorted(matches, key=lambda m: m.score, reverse=True)

    async def _create(
        self, name: str, entity_type: EntityType, is_provisional: bool
    ) -> Entity:
        return await self._repository.create(
            entity_id=EntityId(uuid4()),
            name=name,
            entity_type=entity_type,
            created_at=self._clock.now(),
            is_provisional=is_provisional,
        )
