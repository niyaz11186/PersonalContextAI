"""IntentRouter.

The two properties worth pinning are opposites of each other:

  * the prefilter must catch obvious commands without a model call (D-4, latency)
  * the prefilter must NOT catch ordinary conversation that merely contains
    command-shaped words (correctness)

The second is the one that degrades the product if it breaks, because a false
positive routes a real question into a workflow that cannot answer it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from pca.domain.enums import Intent
from pca.domain.ids import ConversationId
from pca.orchestration.intent_router import IntentRouter, _IntentGuess
from tests.fakes.llm import FakeLLMProvider

pytestmark = pytest.mark.asyncio

CONVERSATION = ConversationId(uuid4())


def _router(**kwargs) -> tuple[IntentRouter, FakeLLMProvider]:
    provider = FakeLLMProvider(**kwargs)
    return IntentRouter(provider, model="small"), provider


# ------------------------------------------------------------------ prefilter


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("That's not right, she moved in March", Intent.CORRECT),
        ("No, I said Bangalore", Intent.CORRECT),
        ("Actually I left that job last year", Intent.CORRECT),
        ("That's not what I said", Intent.CORRECT),
        ("Forget that", Intent.FORGET),
        ("Forget about the dentist appointment", Intent.FORGET),
        ("What did I think about the offer in March?", Intent.HISTORICAL),
        ("What has changed since January?", Intent.HISTORICAL),
    ],
)
async def test_obvious_commands_route_without_a_model_call(
    message: str, expected: Intent
) -> None:
    router, provider = _router()

    decision = await router.classify(message, CONVERSATION)

    assert decision.intent is expected
    assert decision.is_confident
    assert not decision.consulted_model
    assert provider.calls == [], "prefilter should not have cost a model call"


@pytest.mark.parametrize(
    "message",
    [
        "I'll never forget that trip to Goa, it was wonderful and I still think "
        "about the food every week",
        "She said something was wrong with the boiler",
        "Actually is a word I overuse",
    ],
)
async def test_conversation_containing_command_words_is_not_prefiltered(
    message: str,
) -> None:
    """False positives are the expensive direction.

    Escalating an obvious message wastes a call. Matching ordinary conversation sends
    a real question to a workflow that cannot answer it.
    """
    router, provider = _router(
        structured_results=[
            _IntentGuess(intent="converse", confidence=0.9, rationale="ordinary")
        ]
    )

    decision = await router.classify(message, CONVERSATION)

    assert decision.intent is Intent.CONVERSE
    assert provider.calls, "should have escalated rather than prefiltered"


async def test_a_long_message_opening_with_forget_words_escalates() -> None:
    """FORGET is the riskiest prefilter: a false positive refuses to answer at all."""
    router, provider = _router(
        structured_results=[
            _IntentGuess(intent="converse", confidence=0.85, rationale="reminiscing")
        ]
    )

    decision = await router.classify(
        "Forget about the dentist for a moment, I wanted to tell you what happened "
        "at work today because it was quite a strange afternoon",
        CONVERSATION,
    )

    assert decision.intent is Intent.CONVERSE
    assert decision.consulted_model


# ----------------------------------------------------------------- escalation


async def test_an_ambiguous_message_consults_the_model() -> None:
    router, provider = _router(
        structured_results=[
            _IntentGuess(intent="correct", confidence=0.8, rationale="revising a fact")
        ]
    )

    decision = await router.classify("Hmm, about Priya — it was Pune", CONVERSATION)

    assert decision.intent is Intent.CORRECT
    assert decision.consulted_model
    assert len(provider.calls) == 1


async def test_low_confidence_routes_to_clarification_rather_than_guessing() -> None:
    """The property the whole design exists for.

    Acting on a coin-flip classification leaves the wrong memory in place while the
    reply implies it was fixed, and the user gets no signal that nothing changed.
    """
    router, _ = _router(
        structured_results=[
            _IntentGuess(intent="correct", confidence=0.35, rationale="could be either")
        ]
    )

    decision = await router.classify("that one, the other thing", CONVERSATION)

    assert decision.intent is Intent.CLARIFY
    assert not decision.is_confident


async def test_an_unrecognised_intent_becomes_conversation() -> None:
    """A malformed classifier response says nothing about the user's message.

    Interrogating them about our own parsing failure would be confusing and useless,
    so this defaults to CONVERSE rather than CLARIFY.
    """
    router, _ = _router(
        structured_results=[
            _IntentGuess(intent="teleport", confidence=0.95, rationale="?")
        ]
    )

    decision = await router.classify("something", CONVERSATION)

    assert decision.intent is Intent.CONVERSE


async def test_a_dead_classifier_does_not_interrogate_the_user() -> None:
    """Defaulting to CLARIFY here would question the user on every turn while the
    provider is unwell, which is worse than the behaviour of the previous four units.
    """
    router, _ = _router(fail_with=RuntimeError("gemini down"))

    decision = await router.classify("tell me about my week", CONVERSATION)

    assert decision.intent is Intent.CONVERSE
    assert decision.confidence == 0.0


# -------------------------------------------------------------- clarification


async def test_an_open_clarification_captures_the_next_message() -> None:
    """D-3 in-band detection.

    Checked before the prefilter because an answer can look like any other intent:
    "no, the one from work" would otherwise match the correction pattern.
    """
    router, provider = _router()

    decision = await router.classify(
        "No, the one from work", CONVERSATION, has_open_clarification=True
    )

    assert decision.intent is Intent.CLARIFY
    assert decision.confidence == 1.0
    assert provider.calls == []


async def test_an_empty_message_is_conversation_not_a_model_call() -> None:
    router, provider = _router()

    decision = await router.classify("   ", CONVERSATION)

    assert decision.intent is Intent.CONVERSE
    assert provider.calls == []
