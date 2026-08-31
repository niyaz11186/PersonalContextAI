"""IntentRouter — which workflow should handle this message.

Layer L2.

Routing wrongly is not a neutral mistake. Sending a correction down the conversation
path leaves the wrong memory in place while the reply implies it was fixed, and the
user has no signal that nothing changed. That asymmetry is why `classify` returns a
confidence rather than a bare intent, and why anything below the threshold routes to
CLARIFY: asking is cheap, and confidently answering the wrong question is not.

Two-stage by design (D-4). A deterministic prefilter handles the phrasings that are
not genuinely ambiguous, and only what survives it costs a model call. The
alternative — classifying every message with the small model — adds roughly 1.7 s to
every single turn to disambiguate messages that were never ambiguous.

The prefilter is deliberately conservative in one direction. Escalating an obvious
message to the model wastes a call. Matching a message that was not actually a
command routes real conversation into a workflow that cannot answer it, so patterns
are anchored at the start of the message and the riskiest intent (FORGET) also
requires the message be short. "I'll never forget that trip" is not a deletion
request.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from pca.domain.enums import Intent
from pca.domain.ids import ConversationId
from pca.domain.orchestration import RoutingDecision
from pca.observability.logging import get_logger
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage

_log = get_logger(__name__)

# Below this, route to clarification instead of acting. Matches
# RoutingDecision.is_confident.
CONFIDENCE_THRESHOLD = 0.6

# A deletion request is short. This bound is what keeps an incidental "forget" inside
# a longer sentence from being read as a command.
_FORGET_MAX_WORDS = 12

# Every alternation group ends in \b. Without it `(i|it|she)` matches the "i" inside
# "is", so "Actually is a word I overuse" prefilters as a correction — caught by
# test_conversation_containing_command_words_is_not_prefiltered.
_PREFILTER: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        Intent.CORRECT,
        re.compile(
            r"^(that'?s (not right|wrong|incorrect)\b"
            r"|no,? (that'?s|i said|it was)\b"
            r"|actually,? (i|it|she|he|they)\b"
            r"|i meant\b"
            r"|correction[:,]"
            r"|that'?s not what i (said|meant)\b)",
        ),
    ),
    (
        Intent.FORGET,
        re.compile(
            r"^(forget (that|about|what i)\b"
            r"|delete (that|this) (memory|fact)\b"
            r"|remove that (memory|fact)\b)",
        ),
    ),
    (
        Intent.HISTORICAL,
        re.compile(
            r"^(what did i (think|believe|used to)\b"
            r"|what was (true|going on|happening)\b"
            r"|what (has )?changed (since|between)\b"
            r"|back (in|when)\b"
            r"|how did (things|it) (change|evolve)\b)",
        ),
    ),
)

_SYSTEM = """\
Classify what the user wants from a personal-memory assistant.

converse   - ordinary conversation, including questions about remembered facts
correct    - they are saying something you recorded is wrong, or has changed
forget     - they are asking you to delete something you remember
historical - they are asking about the past: what was true then, what they used to
             think, or what has changed over time

Report your confidence honestly. A low confidence causes the system to ask the user
rather than guess, which is the correct outcome when a message is genuinely
ambiguous. Do not inflate confidence to seem decisive.
"""


class _IntentGuess(BaseModel):
    intent: str = Field(description="converse, correct, forget, or historical")
    confidence: float = Field(description="0.0 to 1.0, honestly reported")
    rationale: str = Field(description="One short clause.")


class IntentRouter:
    def __init__(
        self,
        provider: LLMProviderPort,
        model: str | None = None,
        threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self._provider = provider
        # The small model. This is a short classification, the same shape of work as
        # salience and conflict detection, and it sits on the response path.
        self._model = model
        self._threshold = threshold

    async def classify(
        self,
        message: str,
        conversation_id: ConversationId,
        has_open_clarification: bool = False,
    ) -> RoutingDecision:
        """Choose the workflow for this message.

        `has_open_clarification` is the in-band half of D-3: when the system has
        already asked a question and is waiting, the next message is overwhelmingly
        the answer. Checked before anything else, because a clarification answer can
        look like any other intent — "no, the one from work" would otherwise
        prefilter as a correction.
        """
        if has_open_clarification:
            return RoutingDecision(
                intent=Intent.CLARIFY,
                confidence=1.0,
                rationale="a clarification is open in this conversation",
            )

        normalised = message.strip().casefold()
        if not normalised:
            return RoutingDecision(
                intent=Intent.CONVERSE, confidence=1.0, rationale="empty message"
            )

        for intent, pattern in _PREFILTER:
            if not pattern.search(normalised):
                continue
            if intent is Intent.FORGET and len(normalised.split()) > _FORGET_MAX_WORDS:
                # Long message that merely opens with deletion-shaped words. Let the
                # model look at it rather than refusing to answer.
                break
            return RoutingDecision(
                intent=intent,
                confidence=0.9,
                rationale=f"matched the {intent.value} prefilter",
            )

        return await self._ask_model(message, conversation_id)

    # --------------------------------------------------------------- internals

    async def _ask_model(
        self, message: str, conversation_id: ConversationId
    ) -> RoutingDecision:
        prompt = Prompt(
            system=_SYSTEM,
            messages=[PromptMessage(role="user", content=message)],
            temperature=0.0,
        )
        try:
            guess = await self._provider.structured(
                prompt, _IntentGuess, model=self._model
            )
        except Exception as exc:  # noqa: BLE001
            # Falling back to CLARIFY would interrogate the user every time the
            # provider is unwell. CONVERSE is the safe default: it is what the
            # system did for four units, and the correction and historical paths
            # remain reachable through their explicit endpoints.
            _log.warning(
                "intent_classification_failed",
                conversation_id=str(conversation_id),
                error=str(exc)[:200],
                consequence="defaulting to conversation",
            )
            return RoutingDecision(
                intent=Intent.CONVERSE,
                confidence=0.0,
                rationale="classifier unavailable",
                consulted_model=True,
            )

        intent = _parse_intent(guess.intent)
        confidence = max(0.0, min(1.0, guess.confidence))

        if confidence < self._threshold:
            _log.info(
                "intent_below_threshold",
                conversation_id=str(conversation_id),
                proposed=intent.value,
                confidence=round(confidence, 2),
            )
            return RoutingDecision(
                intent=Intent.CLARIFY,
                confidence=confidence,
                rationale=f"unsure between intents: {guess.rationale}",
                consulted_model=True,
            )

        return RoutingDecision(
            intent=intent,
            confidence=confidence,
            rationale=guess.rationale,
            consulted_model=True,
        )


def _parse_intent(raw: str) -> Intent:
    """Map the model's string onto the enum.

    Unrecognised values become CONVERSE, not CLARIFY. A malformed classifier response
    says nothing about whether the user's message was ambiguous, and interrogating
    them about our own parsing failure would be both confusing and useless.
    """
    try:
        return Intent(raw.strip().casefold())
    except ValueError:
        _log.warning("intent_unrecognised", raw=raw, defaulted_to=Intent.CONVERSE.value)
        return Intent.CONVERSE
