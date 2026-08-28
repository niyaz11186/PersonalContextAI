"""Transaction scoping shared by the PostgreSQL repositories.

Layer L5.

Unit 3 makes memory mutation atomic. A supersession writes the replacement fact, ends
the original's world validity, appends two belief transitions, and appends an
operation-log row. If any of those lands without the others the timeline is corrupt in
a way no later read can detect: a fact marked superseded with no record of why, or a
belief transition describing a fact that was never written.

Unit 2 had no such boundary, and it showed. A live commit wrote facts and entities,
then failed on relationships, leaving a half-written episode behind.

The mechanism is deliberately small. `RelationalStorePort` and `Transaction` expose
the same three methods (`execute`, `fetch_all`, `fetch_one`), so a repository can be
handed either one and does not need to know which it has. The only real decision is
whether to join a caller's transaction or open one, and that is what `scope` answers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from pca.ports.store import RelationalStorePort, Transaction


@asynccontextmanager
async def scope(store: RelationalStorePort, tx: Transaction | None):
    """Join the caller's transaction, or open a private one.

    Passing `tx` makes the repository call part of a larger atomic unit. Passing
    `None` keeps the pre-Unit-3 behaviour, which matters for the many single-statement
    reads and writes that genuinely stand alone — wrapping those in a caller-managed
    transaction would be ceremony without benefit.

    Note that this yields the caller's transaction *without* committing it. Commit is
    the outer scope's responsibility; a nested commit here would defeat the atomicity
    this exists to provide.
    """
    if tx is not None:
        yield tx
        return
    async with store.transaction() as own:
        yield own
