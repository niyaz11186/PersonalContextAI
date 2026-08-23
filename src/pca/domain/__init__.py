"""Layer L0 — pure domain types.

Import rule: this package may import from the standard library only. Nothing
here may import from ports, adapters, services, orchestration, or api. Keeping
it dependency-free is what allows every layer above to share these types without
creating a cycle.
"""
