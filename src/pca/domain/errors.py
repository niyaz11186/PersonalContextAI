"""Domain errors.

Layer L0. Standard library only.
"""


class PcaError(Exception):
    """Base for all application errors."""


class ConfigurationError(PcaError):
    """Configuration missing or invalid.

    Raised at startup rather than at first use, so a missing API key fails the
    boot with a clear message instead of surfacing mid-conversation.
    """


class SourceOfRecordUnavailable(PcaError):
    """PostgreSQL is unreachable.

    Deliberately not degradable (constraint C-22). For writes, accepting a
    message the system cannot durably store breaks the product's core promise.
    For reads, answering from Neo4j alone would mean answering from the store
    ADR-015 designates non-authoritative — the exact silent-wrongness the
    architecture exists to prevent.
    """


class MemoryGraphUnavailable(PcaError):
    """Neo4j or Graphiti is unreachable.

    This one IS degradable: retrieval falls back to full-text over source
    messages with a disclosure to the user (NFR-06.5).
    """


class ProviderUnavailable(PcaError):
    """The LLM provider is unreachable after retries."""


class ExtractionTimeout(PcaError):
    """The per-conversation extraction barrier timed out (ADR-008).

    Callers proceed with a degradation notice rather than blocking indefinitely.
    """


class TemporalResolutionError(PcaError):
    """A time reference could not be resolved and the caller required one.

    Normal unresolvable references do not raise — they yield UNKNOWN granularity
    with null dates. This is only for callers that treat resolution as mandatory.
    """


class MemoryNotFound(PcaError):
    """A correction, supersession, or retraction named a memory that does not exist.

    Raised rather than ignored. These operations are always deliberate — a user or
    operator asserting something about a specific record — so silently doing nothing
    would leave them believing a change had been applied when it had not.
    """


class ClarificationNotFound(PcaError):
    """A resume named a clarification thread with no checkpoint.

    Distinct from an already-answered one. A missing thread means the interrupt was
    never recorded or its checkpoints were deleted, and answering into nothing would
    tell the user their clarification was applied when no write occurred.
    """
