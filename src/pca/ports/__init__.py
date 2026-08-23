"""Layer L4 — abstract ports.

Import rule: may import from `pca.domain` only, plus typing and pydantic for
schema declarations. No third-party SDK types may appear in any signature here —
that is what keeps adapters replaceable.
"""

from pca.ports.clock import ClockPort
from pca.ports.graph import EntityDivergence, GraphHit, GraphIngestResult, MemoryGraphPort
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage, ProviderHealth
from pca.ports.objects import ObjectStorePort
from pca.ports.store import RelationalStorePort, Transaction

__all__ = [
    "ClockPort",
    "EntityDivergence",
    "GraphHit",
    "GraphIngestResult",
    "LLMProviderPort",
    "MemoryGraphPort",
    "ObjectStorePort",
    "Prompt",
    "PromptMessage",
    "ProviderHealth",
    "RelationalStorePort",
    "Transaction",
]
