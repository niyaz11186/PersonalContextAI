"""Unit 4 — retrieval depth.

The completion criterion: a question that requires history returns a SMALL, RELEVANT
context package, and diagnostics show which strategies contributed and what the
governor discarded.

"Small" is the part that is easy to fail while looking successful. Returning
everything similar also returns the right answer, so a test that only checks the
answer is present cannot distinguish good retrieval from a dump. The assertions below
therefore check both halves: the relevant fact is present AND the package is bounded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from pca.domain.enums import Confidence, EntityType, MemoryKind, Origin
from pca.domain.ids import ConversationId, EntityId, EpisodeId, MemoryId, MessageId
from pca.domain.memory import Fact, ProvenanceRef
from pca.domain.retrieval import (
    RetrievalBudget,
    RetrievalQuery,
    RetrievalStrategy,
    Spend,
)
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.services.budget import (
    MAX_RERANK_CANDIDATES,
    RetrievalBudgetGovernor,
)
from pca.services.context_assembly import ContextAssemblyService
from pca.services.fusion import RRF_K, fuse
from pca.services.retrieval import RetrievalService
from tests.fakes.graph import FakeMemoryGraph
from tests.fakes.memory_repositories import (
    FakeEntityRepository,
    FakeMemoryRepository,
)
from pca.ports.graph import GraphHit

JANUARY = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def ref() -> ProvenanceRef:
    return ProvenanceRef(
        episode_id=EpisodeId(uuid4()),
        conversation_id=ConversationId(uuid4()),
        message_id=MessageId(uuid4()),
    )


def fact(
    statement: str,
    *,
    origin: Origin = Origin.USER_STATED,
    confidence: Confidence = Confidence.CERTAIN,
    salience: float = 0.5,
    subjects: list[EntityId] | None = None,
    valid_from: datetime | None = None,
    supersedes: MemoryId | None = None,
    corrected_from: MemoryId | None = None,
) -> Fact:
    return Fact(
        id=MemoryId(uuid4()),
        statement=statement,
        origin=origin,
        confidence=confidence,
        validity=TemporalValidity(valid_from=valid_from),
        belief=BeliefWindow(asserted_at=JANUARY),
        provenance=[ref()],
        salience=salience,
        subject_entity_ids=subjects or [],
        supersedes=supersedes,
        corrected_from=corrected_from,
    )


# ------------------------------------------------------------------- fusion


def test_rrf_prefers_agreement_over_a_single_confident_hit() -> None:
    """The central property of RRF, and the reason it was chosen.

    A hit found at mediocre rank by three strategies should outrank one found first
    by exactly one. Score-based fusion cannot express this — it would just pick the
    biggest number, which is whichever scale happens to be largest.
    """
    lonely = GraphHit(ref="lonely", content="x", score=0.99)
    agreed = GraphHit(ref="agreed", content="y", score=0.10)

    fused = fuse(
        [
            ("semantic", [lonely, agreed]),
            ("fulltext", [agreed]),
            ("entity", [agreed]),
        ]
    )

    assert [h.ref for h in fused][0] == "agreed", (
        "three strategies agreeing must outrank one high score; otherwise fusion is "
        "just score comparison across incomparable scales"
    )


def test_rrf_ignores_score_magnitude() -> None:
    """Ranks only. A strategy emitting huge scores must not dominate."""
    a = GraphHit(ref="a", content="a", score=1000.0)
    b = GraphHit(ref="b", content="b", score=0.001)

    fused = fuse([("semantic", [b, a])])

    assert [h.ref for h in fused] == ["b", "a"], (
        "input order is the rank; scores must not reorder within a strategy"
    )


def test_fusion_deduplicates_by_ref() -> None:
    hit = GraphHit(ref="same", content="c", score=0.5)
    fused = fuse([("semantic", [hit]), ("fulltext", [hit]), ("temporal", [hit])])
    assert len(fused) == 1


def test_fusion_skips_hits_with_no_ref() -> None:
    """A ref-less hit cannot be deduplicated, so it would be counted once per
    strategy and rise to the top on the strength of its own duplicates."""
    anonymous = GraphHit(ref="", content="ghost", score=0.9)
    real = GraphHit(ref="real", content="solid", score=0.1)

    fused = fuse([("semantic", [anonymous, real]), ("fulltext", [anonymous])])

    assert [h.ref for h in fused] == ["real"]


def test_rrf_k_damps_the_top_position() -> None:
    """With k=60 the rank-1 advantage must be small enough that one strategy cannot
    unilaterally decide the winner — that is what makes fusing worthwhile."""
    first_place = 1.0 / (RRF_K + 1)
    two_mediocre = 2 * (1.0 / (RRF_K + 5))
    assert two_mediocre > first_place


# ------------------------------------------------------------------ governor


def test_should_continue_is_not_gated_by_item_count() -> None:
    """Gathering more candidates than we keep is the point: fusion and reranking need
    a surplus to choose from."""
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=5, max_context_chars=8000
        )
    )
    spent = Spend(elapsed=timedelta(seconds=1), chars=100)
    assert governor.should_continue(spent, gathered=500) is True


def test_should_continue_stops_before_the_duration_budget_is_exhausted() -> None:
    """Headroom matters: a stage authorised at 99% of budget still overruns it."""
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=timedelta(seconds=10), max_items=5, max_context_chars=8000
        )
    )
    assert governor.should_continue(Spend(timedelta(seconds=5), 0), 0) is True
    assert governor.should_continue(Spend(timedelta(seconds=9), 0), 0) is False


def test_should_continue_stops_at_the_context_ceiling() -> None:
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=5, max_context_chars=100
        )
    )
    assert governor.should_continue(Spend(timedelta(0), 150), 0) is False
    assert governor.stop_reason(Spend(timedelta(0), 150), 0) is not None
    assert "context ceiling" in governor.stop_reason(Spend(timedelta(0), 150), 0)


def test_stop_reason_is_none_when_continuing() -> None:
    """A governor that stops without saying why is untunable; one that reports a
    reason while continuing is lying."""
    governor = RetrievalBudgetGovernor()
    assert governor.stop_reason(Spend(timedelta(seconds=1), 10), 1) is None


def test_trim_reports_what_it_discarded() -> None:
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=2, max_context_chars=8000
        )
    )
    kept, dropped = governor.trim(["a", "b", "c", "d"], len)
    assert kept == ["a", "b"]
    assert dropped == 2


def test_trim_keeps_one_oversized_item_rather_than_returning_nothing() -> None:
    """An empty context is indistinguishable from a retrieval failure. One
    over-budget item is the lesser evil."""
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=5, max_context_chars=10
        )
    )
    kept, _ = governor.trim(["x" * 500], len)
    assert len(kept) == 1


def test_rerank_cutoff_caps_cross_encoder_calls() -> None:
    """The Gemini reranker costs one API call PER PASSAGE. Without a cap this is the
    dominant latency cost, not a refinement."""
    governor = RetrievalBudgetGovernor()
    assert governor.rerank_cutoff(500) == MAX_RERANK_CANDIDATES
    assert governor.rerank_cutoff(3) == 3


# --------------------------------------------------- retrieval orchestration


def build(
    graph: FakeMemoryGraph | None = None,
    budget: RetrievalBudget | None = None,
) -> tuple[RetrievalService, FakeMemoryGraph, FakeMemoryRepository, FakeEntityRepository]:
    graph = graph or FakeMemoryGraph()
    memory = FakeMemoryRepository()
    entities = FakeEntityRepository()
    service = RetrievalService(
        graph=graph,
        memory=memory,
        entities=entities,
        governor=RetrievalBudgetGovernor(budget) if budget else RetrievalBudgetGovernor(),
    )
    return service, graph, memory, entities


async def test_strategies_run_concurrently_and_are_reported_separately() -> None:
    """Diagnostics must show WHICH strategies contributed — the completion criterion.

    The fake distinguishes semantic (any shared word) from full-text (whole phrase),
    so this cannot pass by running one strategy twice.
    """
    service, graph, memory, _ = build()
    graph.add_hit("h1", "Priya lives in Pune")
    graph.add_hit("h2", "Pune has good weather")
    await memory.insert_fact(fact("Priya lives in Pune"), None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya lives in Pune", budget=service.budget_for())
    )

    contributing = result.diagnostics.contributing_strategies
    assert RetrievalStrategy.SEMANTIC.value in contributing
    assert RetrievalStrategy.FULLTEXT.value in contributing
    assert "semantic" in graph.calls and "fulltext" in graph.calls


async def test_a_failed_strategy_does_not_fail_the_request() -> None:
    """One strategy raising must degrade that strategy, not the whole retrieval."""
    graph = FakeMemoryGraph()
    graph.fail_strategies = {"fulltext"}
    service, graph, memory, _ = build(graph=graph)
    graph.add_hit("h1", "Priya lives in Pune")
    await memory.insert_fact(fact("Priya lives in Pune"), None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya lives in Pune", budget=service.budget_for())
    )

    assert RetrievalStrategy.FULLTEXT.value in result.diagnostics.failed_strategies
    assert RetrievalStrategy.SEMANTIC.value in result.diagnostics.contributing_strategies
    assert result.facts, "a working strategy must still produce results"


async def test_a_failed_strategy_is_distinguished_from_one_that_found_nothing() -> None:
    """`hits == 0` and `failed` are different facts about the system: finding nothing
    and being unable to look require different responses."""
    graph = FakeMemoryGraph()
    graph.fail_strategies = {"semantic"}
    service, graph, _, _ = build(graph=graph)

    result = await service.retrieve(
        RetrievalQuery(text="nothing here", budget=service.budget_for())
    )

    spends = {s.strategy: s for s in result.diagnostics.spends}
    assert spends[RetrievalStrategy.SEMANTIC.value].failed is True
    assert spends[RetrievalStrategy.FULLTEXT.value].failed is False
    assert spends[RetrievalStrategy.FULLTEXT.value].hits == 0


async def test_any_strategy_failure_discloses_degradation() -> None:
    """NFR-06.5. Erring toward disclosure: each strategy exists to catch what the
    others miss, so losing one genuinely means context may be absent."""
    graph = FakeMemoryGraph()
    graph.fail_strategies = {"semantic"}
    service, _, _, _ = build(graph=graph)

    result = await service.retrieve(
        RetrievalQuery(text="anything", budget=service.budget_for())
    )
    assert result.diagnostics.degraded is True


async def test_entity_scoped_search_uses_names_not_ids() -> None:
    """Graphiti's node ids are assigned by its own extraction pass and are not our
    EntityIds. Passing an EntityId as a centre node matched nothing and silently
    returned unscoped results."""
    service, graph, memory, entities = build()
    priya = await entities.create(
        entity_id=EntityId(uuid4()),
        name="Priya",
        entity_type=EntityType.PERSON,
        created_at=JANUARY,
    )
    graph.add_hit("h1", "Priya lives in Pune", entity_names=["Priya"])
    await memory.insert_fact(fact("Priya lives in Pune", subjects=[priya.id]), None)

    result = await service.retrieve(
        RetrievalQuery(
            text="where does she live",
            budget=service.budget_for(),
            entity_scope=[priya.id],
        )
    )

    assert RetrievalStrategy.ENTITY.value in result.diagnostics.contributing_strategies
    assert any("Pune" in f.statement for f in result.facts)


async def test_temporal_search_only_runs_when_the_query_is_time_scoped() -> None:
    """Defaulting to all-time would make this a duplicate of semantic search with
    extra latency."""
    service, graph, _, _ = build()
    graph.add_hit("h1", "Priya lives in Pune", valid_from=JANUARY)

    untimed = await service.retrieve(
        RetrievalQuery(text="Priya", budget=service.budget_for())
    )
    assert RetrievalStrategy.TEMPORAL.value not in [
        s.strategy for s in untimed.diagnostics.spends
    ]

    timed = await service.retrieve(
        RetrievalQuery(
            text="Priya", budget=service.budget_for(), time_range=(JANUARY, JUNE)
        )
    )
    assert RetrievalStrategy.TEMPORAL.value in [
        s.strategy for s in timed.diagnostics.spends
    ]


async def test_traversal_is_seeded_from_fused_results_never_blind() -> None:
    """FR-06.5. Traversing from a bad seed floods the package with irrelevance."""
    service, graph, memory, _ = build()
    graph.add_hit("h1", "Priya lives in Pune", source_node="node:priya")
    graph.adjacency["node:priya"] = [
        GraphHit(ref="h2", content="Priya works at Google", score=0.4)
    ]
    await memory.insert_fact(fact("Priya lives in Pune"), None)
    await memory.insert_fact(fact("Priya works at Google"), None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya lives in Pune", budget=service.budget_for())
    )

    assert "traversal" in graph.calls
    assert RetrievalStrategy.TRAVERSAL.value in [
        s.strategy for s in result.diagnostics.spends
    ]
    statements = {f.statement for f in result.facts}
    assert "Priya works at Google" in statements, (
        "traversal should surface the connected fact nobody asked about directly"
    )


async def test_traversal_does_not_run_without_seeds() -> None:
    """No fused hits means no seed. Traversing anyway would be the blind sweep
    FR-06.5 forbids."""
    service, graph, _, _ = build()

    await service.retrieve(
        RetrievalQuery(text="nothing matches", budget=service.budget_for())
    )
    assert "traversal" not in graph.calls


async def test_governor_stops_traversal_when_the_budget_is_spent() -> None:
    """Traversal is a second round trip, so it is the first thing sacrificed."""
    service, graph, _, _ = build(
        budget=RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=5, max_context_chars=1
        )
    )
    graph.add_hit("h1", "Priya lives in Pune", source_node="node:priya")
    graph.adjacency["node:priya"] = [GraphHit(ref="h2", content="more", score=0.4)]

    result = await service.retrieve(
        RetrievalQuery(text="Priya lives in Pune", budget=service.budget_for())
    )

    assert result.diagnostics.stopped_early is True
    assert result.diagnostics.stop_reason is not None
    assert "traversal" not in graph.calls


async def test_stopped_early_is_distinct_from_dropped_by_budget() -> None:
    """Halting retrieval and trimming results are different behaviours; conflating
    them hides which one the budget actually triggered."""
    service, graph, memory, _ = build(
        budget=RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=1, max_context_chars=8000
        )
    )
    for i in range(4):
        graph.add_hit(f"h{i}", f"Priya fact number {i}")
        await memory.insert_fact(fact(f"Priya fact number {i}"), None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya fact", budget=service.budget_for())
    )

    assert result.diagnostics.stopped_early is False
    assert result.diagnostics.dropped_by_budget > 0


async def test_the_package_is_bounded_by_max_items() -> None:
    """The 'small' half of the completion criterion. Returning everything similar
    also returns the right answer, so smallness must be asserted separately."""
    service, graph, memory, _ = build(
        budget=RetrievalBudget(
            max_duration=timedelta(seconds=25), max_items=3, max_context_chars=8000
        )
    )
    for i in range(25):
        graph.add_hit(f"h{i}", f"Priya detail {i}")
        await memory.insert_fact(fact(f"Priya detail {i}"), None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya detail", budget=service.budget_for())
    )

    assert len(result.facts) <= 3, "retrieval must return the smallest useful set"
    assert result.diagnostics.dropped_by_budget > 0


async def test_reranking_is_capped(monkeypatch) -> None:
    """One API call per passage means an uncapped rerank is a latency blowout."""
    service, graph, memory, _ = build()
    for i in range(40):
        graph.add_hit(f"h{i}", f"Priya detail {i}", score=i / 100)
        await memory.insert_fact(fact(f"Priya detail {i}"), None)

    await service.retrieve(
        RetrievalQuery(text="Priya detail", budget=service.budget_for())
    )

    assert graph.reranked_with, "the reranker should have been called"
    _, count = graph.reranked_with[0]
    assert count <= MAX_RERANK_CANDIDATES


async def test_facts_come_from_postgres_not_from_graph_text() -> None:
    """ADR-015: the graph finds candidates, PostgreSQL asserts what is true.

    The graph hit here is Graphiti's paraphrase. The returned fact must be the stored
    record, not the paraphrase — otherwise the system presents the graph's wording as
    the user's remembered fact.
    """
    service, graph, memory, _ = build()
    graph.add_hit("h1", "Priya resides in the city of Pune")
    stored = fact("Priya lives in Pune")
    await memory.insert_fact(stored, None)

    result = await service.retrieve(
        RetrievalQuery(text="Priya Pune", budget=service.budget_for())
    )

    assert [f.statement for f in result.facts] == ["Priya lives in Pune"]
    assert all(f.id == stored.id for f in result.facts)


async def test_unmatched_graph_hits_are_recorded_not_hidden() -> None:
    """Graphiti paraphrases, so some hits will not match a stored statement. A ratio
    near 100% means the text-matching seam has broken and ranking is doing nothing —
    which must be visible."""
    service, graph, memory, _ = build()
    graph.add_hit("h1", "completely different wording entirely")
    await memory.insert_fact(fact("Priya lives in Pune"), None)

    result = await service.retrieve(
        RetrievalQuery(text="different wording", budget=service.budget_for())
    )

    assert any("did not match" in n for n in result.diagnostics.notes)


async def test_committed_facts_are_reachable_even_if_the_graph_never_indexed_them() -> None:
    """Retrieval quality must not be hostage to Graphiti's extraction. A fact we
    committed but it never indexed would otherwise be permanently unreachable."""
    service, _, memory, _ = build()
    await memory.insert_fact(fact("Priya lives in Pune", salience=0.9), None)

    result = await service.retrieve(
        RetrievalQuery(text="anything at all", budget=service.budget_for())
    )

    assert any("Pune" in f.statement for f in result.facts)


async def test_retrieval_without_a_memory_repository_returns_no_facts() -> None:
    """Rather than fabricating facts from graph text, which is what the Unit 1b
    raw_hits channel effectively did."""
    graph = FakeMemoryGraph()
    graph.add_hit("h1", "Priya lives in Pune")
    service = RetrievalService(graph=graph)

    result = await service.retrieve(
        RetrievalQuery(text="Priya", budget=service.budget_for())
    )
    assert result.facts == []


# ------------------------------------------------------- context assembly


def test_the_four_buckets_are_disjoint() -> None:
    """A fact appearing twice would let the model double-count corroboration it does
    not have."""
    facts = [
        fact("user said this", origin=Origin.USER_STATED),
        fact("we inferred this", origin=Origin.AI_INFERRED),
        fact("this is shaky", confidence=Confidence.UNCERTAIN),
        fact("this replaced something", supersedes=MemoryId(uuid4())),
    ]
    buckets = ContextAssemblyService._split(facts)

    all_ids = [
        f.id
        for bucket in buckets.values()
        for f in bucket
    ]
    assert len(all_ids) == len(set(all_ids)) == 4


def test_uncertainty_outranks_origin() -> None:
    """An uncertain fact must not be presented as user-stated just because the user
    said it hesitantly."""
    shaky = fact(
        "maybe Pune", origin=Origin.USER_STATED, confidence=Confidence.UNCERTAIN
    )
    buckets = ContextAssemblyService._split([shaky])

    assert buckets["uncertain"] == [shaky]
    assert buckets[Origin.USER_STATED] == []


def test_history_outranks_origin() -> None:
    """"This superseded something" is more consequential to disclose than who said
    it — it changes what a question about the past should be answered with."""
    replaced = fact(
        "Priya lives in Bangalore",
        origin=Origin.USER_STATED,
        supersedes=MemoryId(uuid4()),
    )
    buckets = ContextAssemblyService._split([replaced])

    assert buckets["currently_believed"] == [replaced]
    assert buckets[Origin.USER_STATED] == []


def test_imported_facts_are_not_treated_as_user_stated() -> None:
    """FR-02.7 forbids blurring an import into the user's own assertion."""
    imported = fact("from a document", origin=Origin.IMPORTED)
    buckets = ContextAssemblyService._split([imported])
    assert buckets[Origin.AI_INFERRED] == [imported]
    assert buckets[Origin.USER_STATED] == []


def test_render_labels_every_section_by_epistemic_status() -> None:
    """FR-07.4. An unlabelled block lets the model treat its own inference as
    something the user asserted."""
    service = ContextAssemblyService()
    from pca.domain.retrieval import ContextPackage

    package = ContextPackage(
        user_stated=[fact("stated")],
        system_derived=[fact("inferred", origin=Origin.AI_INFERRED)],
        currently_believed=[fact("replaced", supersedes=MemoryId(uuid4()))],
        uncertain=[fact("shaky", confidence=Confidence.UNCERTAIN)],
    )
    rendered = service.render(package)

    assert "Stated by the user" in rendered
    assert "Derived by the system" in rendered
    assert "Current state" in rendered
    assert "Uncertain" in rendered
    # The four statements must not bleed into one another's sections.
    assert rendered.index("stated") < rendered.index("replaced")


def test_render_says_so_when_there_is_nothing() -> None:
    """Silence would invite the model to fill the gap with a plausible recollection."""
    service = ContextAssemblyService()
    from pca.domain.retrieval import ContextPackage

    rendered = service.render(ContextPackage())
    assert "no stored history" in rendered
    assert "Do not invent" in rendered


def test_timeline_excludes_undated_facts() -> None:
    """ADR-010 leaves dates null rather than guessing. An undated fact has no
    position, and placing it anywhere would be an invention."""
    timeline = ContextAssemblyService._timeline(
        [fact("dated", valid_from=JANUARY), fact("undated")]
    )
    assert timeline is not None
    assert [e.description for e in timeline.entries] == ["dated"]


def test_timeline_is_chronological() -> None:
    timeline = ContextAssemblyService._timeline(
        [fact("later", valid_from=JUNE), fact("earlier", valid_from=JANUARY)]
    )
    assert timeline is not None
    assert [e.description for e in timeline.entries] == ["earlier", "later"]


async def test_early_stop_is_disclosed_to_the_user() -> None:
    """If the search was cut short, the answer must say so — otherwise a confident
    reply rests on knowingly incomplete context."""
    from pca.domain.retrieval import RetrievalDiagnostics, RetrievalResult

    service = ContextAssemblyService()
    package = await service.assemble(
        result=RetrievalResult(
            diagnostics=RetrievalDiagnostics(stopped_early=True, stop_reason="budget")
        ),
        history=[],
    )

    assert package.degradation_notices
    assert "cut short" in service.render(package)
