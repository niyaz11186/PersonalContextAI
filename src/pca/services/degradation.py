"""DegradationPolicy — what to do when a dependency fails, and what to tell the user.

Layer L3. Pure: no I/O, no dependencies, deterministic.

NFR-06.5 requires graceful degradation *with disclosure*. Those two halves fail
apart very easily. Before this existed, the disclosure sentences lived as string
literals at the point of each `except` clause in the API router, which meant the
degraded paths were invisible as a set — you could not read the code and answer
"what are all the ways this system can answer with less than it should, and what does
it say when it does?" This module is that answer in one place.

Every method returns a `Degradation`, which cannot be constructed without disclosure
text (C-34). A caller can still choose not to show it, but not by accident.

The one case deliberately absent is PostgreSQL. Constraint C-22 gives the system of
record no degradation path at all: a lost write breaks the product's core promise,
and a read served from Neo4j alone would come from the store ADR-015 designates
non-authoritative. That failure raises; it does not degrade.
"""

from __future__ import annotations

from pca.domain.enums import DegradationAction
from pca.domain.ids import ConversationId
from pca.domain.orchestration import Degradation
from pca.observability.logging import get_logger

_log = get_logger(__name__)


class DegradationPolicy:
    def on_retrieval_failure(self, error: Exception) -> Degradation:
        """Memory could not be searched. Answer from the conversation alone.

        The disclosure matters more here than anywhere else. Without it the reply is
        indistinguishable from one where the system genuinely remembered nothing
        relevant — and the user's reasonable conclusion, that they never mentioned it,
        is exactly wrong. A personal-context assistant that quietly forgets is worse
        than one that admits it cannot reach its memory.
        """
        _log.error(
            "retrieval_degraded",
            error=str(error)[:300],
            consequence="answering from conversation only; user disclosed",
        )
        return Degradation(
            action=DegradationAction.PROCEED_WITHOUT_MEMORY,
            disclosure=(
                "I could not reach my memory just now, so this answer uses only our "
                "current conversation. There may be things I have recorded that I am "
                "not seeing."
            ),
            cause=type(error).__name__,
        )

    def on_extraction_timeout(self, conversation_id: ConversationId) -> Degradation:
        """The write barrier gave up waiting (ADR-008).

        ADR-008 is explicit: proceed rather than block forever, record the event, and
        tell the user recent context may be incomplete. The abandoned extraction stays
        durable and is retried by `recover_pending`, so nothing is lost — but the
        reply about to be generated may not include the last thing they said, and that
        is precisely the case where staying silent would mislead.
        """
        _log.warning(
            "extraction_barrier_timeout",
            conversation_id=str(conversation_id),
            consequence="proceeding; the most recent message may not be in memory yet",
        )
        return Degradation(
            action=DegradationAction.PROCEED_WITH_INCOMPLETE_MEMORY,
            disclosure=(
                "I am still processing what you told me a moment ago, so I may not "
                "have it available yet."
            ),
            cause="extraction_barrier_timeout",
        )

    def on_provider_unavailable(self, error: Exception) -> Degradation:
        """Gemini is unreachable after retries (NFR-06.1).

        There is no fallback provider (C-11), so there is no degraded answer to give —
        only an honest failure. `FAIL_REQUEST` here is not an escape from NFR-06.5:
        the requirement is to degrade gracefully where degradation is possible, and a
        fabricated reply would be the opposite of graceful.

        The disclosure states the message was saved, because it was — the durability
        point precedes every model call.
        """
        _log.error("provider_unavailable", error=str(error)[:300])
        return Degradation(
            action=DegradationAction.FAIL_REQUEST,
            disclosure=(
                "I cannot reach the language model right now, so I am unable to "
                "reply. Your message was saved."
            ),
            cause=type(error).__name__,
        )

    def on_memory_write_failure(self, error: Exception) -> Degradation:
        """Extraction or commit failed after the reply was delivered.

        The conversation is unaffected — the message is durable and the answer was
        already sent. What is affected is the future: this exchange will not be
        searchable until recovery runs. Disclosed because the user may be relying on
        having just told the system something important.
        """
        _log.error(
            "memory_write_degraded",
            error=str(error)[:300],
            consequence="message saved; not searchable until recovery",
        )
        return Degradation(
            action=DegradationAction.PROCEED_WITH_INCOMPLETE_MEMORY,
            disclosure=(
                "Your message was saved, but I could not add it to my memory just "
                "now. I will retry it."
            ),
            cause=type(error).__name__,
        )

    def on_graph_unavailable(self, error: Exception) -> Degradation:
        """Neo4j or Graphiti is down.

        Degradable by design (ADR-005): the graph is a rebuildable projection, and
        PostgreSQL still holds every fact. Retrieval loses its candidate-generation
        stage and falls back to what can be resolved directly, which is narrower but
        not wrong.
        """
        _log.error(
            "graph_degraded",
            error=str(error)[:300],
            consequence="retrieval narrowed to direct lookups; user disclosed",
        )
        return Degradation(
            action=DegradationAction.PROCEED_WITH_INCOMPLETE_MEMORY,
            disclosure=(
                "Part of my memory search is unavailable, so I may be missing "
                "connections I would normally make."
            ),
            cause=type(error).__name__,
        )
